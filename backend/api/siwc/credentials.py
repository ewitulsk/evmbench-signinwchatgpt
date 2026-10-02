"""Encrypted storage for ChatGPT OAuth tokens, and the refresh broker used by the API and oai_proxy."""

import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Literal

from loguru import logger
from sqlalchemy import select, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from api.models.chatgpt_credential import AppSetting, ChatGPTCredential
from api.models.job import Job
from api.siwc.errors import UNUSABLE_TOKEN_ERRORS
from api.siwc.oauth import PLAN_USAGE_SCOPE, OAuthError, SiwcOAuthClient, TokenSet, parse_scopes
from api.util.aes_gcm import decrypt_token, derive_key, encrypt_token


# Refresh this long before the 1 h access token expires so in-flight streams don't race expiry.
REFRESH_MARGIN = timedelta(minutes=5)
EXT_AGENT_HOST_ID_SETTING = 'siwc.ext_agent_host_id'


class PlanUsageUnavailableError(Exception):
    """No credential, or the user didn't grant ChatGPT plan usage."""


class ReauthRequiredError(Exception):
    """The stored credential can't be refreshed; the user must sign in with ChatGPT again."""


PlanUsageState = Literal['connected', 'declined', 'reauth_required', 'unavailable']


@dataclass(frozen=True)
class PlanUsageStatus:
    state: PlanUsageState


def _now() -> datetime:
    return datetime.now(tz=UTC)


class CredentialStore:
    def __init__(self, secret: str) -> None:
        self._key = derive_key(secret + ':chatgpt-credentials')

    def _encrypt(self, value: str | None) -> str | None:
        return encrypt_token(value, key=self._key) if value else None

    def _decrypt(self, value: str | None) -> str | None:
        return decrypt_token(value, key=self._key) if value else None

    async def save(  # noqa: PLR0913
        self,
        session: AsyncSession,
        *,
        user_id: str,
        subject: str,
        email: str | None,
        client_id: str,
        ext_agent_host_id: str | None,
        tokens: TokenSet,
    ) -> ChatGPTCredential:
        credential = await session.get(ChatGPTCredential, user_id)
        if credential is None:
            credential = ChatGPTCredential(user_id=user_id)
            session.add(credential)
        credential.subject = subject
        credential.email = email
        credential.client_id = client_id
        credential.ext_agent_host_id = ext_agent_host_id
        self._apply_tokens(credential, tokens)
        await session.commit()
        return credential

    def _apply_tokens(self, credential: ChatGPTCredential, tokens: TokenSet) -> None:
        credential.access_token_enc = self._encrypt(tokens.access_token) or ''
        # Refresh responses rotate the refresh token; keep the old one only if none was returned.
        if tokens.refresh_token:
            credential.refresh_token_enc = self._encrypt(tokens.refresh_token)
        if tokens.id_token:
            credential.id_token_enc = self._encrypt(tokens.id_token)
        if tokens.scopes:
            credential.scopes = ' '.join(tokens.scopes)
        credential.access_expires_at = tokens.expires_at
        credential.earliest_refresh_at = tokens.earliest_refresh_at
        credential.needs_reauth = False

    def id_token(self, credential: ChatGPTCredential) -> str | None:
        return self._decrypt(credential.id_token_enc)

    @staticmethod
    def plan_usage_status(credential: ChatGPTCredential | None) -> PlanUsageStatus:
        if credential is None:
            return PlanUsageStatus('unavailable')
        if credential.needs_reauth or not credential.access_token_enc:
            return PlanUsageStatus('reauth_required')
        if PLAN_USAGE_SCOPE not in parse_scopes(credential.scopes):
            return PlanUsageStatus('declined')
        return PlanUsageStatus('connected')

    @staticmethod
    def _is_fresh(credential: ChatGPTCredential, now: datetime) -> bool:
        return bool(credential.access_token_enc) and credential.access_expires_at - now > REFRESH_MARGIN

    async def get_access_token(self, session: AsyncSession, *, user_id: str, oauth: SiwcOAuthClient) -> str:
        """Return a usable access token, refreshing it (serialized per user) when close to expiry."""
        credential = await session.get(ChatGPTCredential, user_id)
        status = self.plan_usage_status(credential)
        if status.state == 'reauth_required':
            raise ReauthRequiredError
        if credential is None or status.state != 'connected':
            raise PlanUsageUnavailableError

        now = _now()
        if self._is_fresh(credential, now):
            return self._decrypt(credential.access_token_enc) or ''

        return await self._refresh_locked(session, user_id=user_id, oauth=oauth)

    async def _refresh_locked(self, session: AsyncSession, *, user_id: str, oauth: SiwcOAuthClient) -> str:
        # Refresh tokens rotate and can't be reused, so only one refresher per user at a time. The row lock
        # serializes API workers and oai_proxy workers alike; everyone else re-reads the rotated tokens.
        credential = await session.scalar(
            select(ChatGPTCredential)
            .where(ChatGPTCredential.user_id == user_id)
            .with_for_update()
            .execution_options(populate_existing=True),
        )
        if credential is None:
            raise PlanUsageUnavailableError
        if credential.needs_reauth:
            await session.commit()
            raise ReauthRequiredError

        now = _now()
        not_expired = credential.access_expires_at > now + timedelta(seconds=30)
        too_early = credential.earliest_refresh_at is not None and now < credential.earliest_refresh_at
        if self._is_fresh(credential, now) or (not_expired and too_early):
            # Another worker refreshed while we waited for the lock, or the server asked us to hold off.
            await session.commit()
            return self._decrypt(credential.access_token_enc) or ''

        refresh_token = self._decrypt(credential.refresh_token_enc)
        if not refresh_token:
            credential.needs_reauth = True
            await session.commit()
            raise ReauthRequiredError

        try:
            tokens = await oauth.refresh(client_id=credential.client_id, refresh_token=refresh_token)
        except OAuthError as err:
            if err.code in UNUSABLE_TOKEN_ERRORS:
                logger.warning(f'ChatGPT refresh token unusable for user_id={user_id}: {err.code}')
                credential.needs_reauth = True
                await session.commit()
                raise ReauthRequiredError from err
            await session.rollback()
            if err.code == 'invalid_client':
                logger.error('ChatGPT token refresh failed with invalid_client; check AUTH_BACKEND_ARGUMENTS')
            raise

        self._apply_tokens(credential, tokens)
        await session.commit()
        return tokens.access_token

    async def sign_out(self, session: AsyncSession, *, user_id: str, oauth: SiwcOAuthClient) -> None:
        """Revoke the refresh token and drop tokens; keep client_id + id_token for `id_token_hint` re-sign-in."""
        credential = await session.get(ChatGPTCredential, user_id)
        if credential is None:
            return
        refresh_token = self._decrypt(credential.refresh_token_enc)
        if refresh_token:
            await oauth.revoke(client_id=credential.client_id, refresh_token=refresh_token)
        credential.access_token_enc = ''
        credential.refresh_token_enc = None
        credential.needs_reauth = True
        await session.commit()


async def get_ext_agent_host_id(session: AsyncSession, *, override: str | None = None) -> str:
    """Stable per-deployment host identifier required by dynamic SIWC registration."""
    if override:
        return override
    await session.execute(
        insert(AppSetting)
        .values(key=EXT_AGENT_HOST_ID_SETTING, value=f'urn:uuid:{uuid.uuid4()}')
        .on_conflict_do_nothing(index_elements=['key']),
    )
    value = await session.scalar(select(AppSetting.value).where(AppSetting.key == EXT_AGENT_HOST_ID_SETTING))
    await session.commit()
    if not value:
        msg = 'Unable to persist ext_agent_host_id'
        raise RuntimeError(msg)
    return value


async def record_job_error_code(session: AsyncSession, *, job_id: uuid.UUID, code: str) -> None:
    """Attach a machine-readable failure reason to a job; the first code recorded wins."""
    await session.execute(
        update(Job).where(Job.id == job_id, Job.error_code.is_(None)).values(error_code=code[:64]),
    )
    await session.commit()
