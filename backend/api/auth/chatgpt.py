"""Sign in with ChatGPT auth backend.

AUTH_BACKEND_ARGUMENTS:
- mode: "dynamic" (default; open-source/self-hosted, loopback redirect on 127.0.0.1) or "registered"
  (commercial client issued by OpenAI; needs client_id, and client_secret for confidential clients)
- client_id / client_secret: registered mode only
- issuer: defaults to https://auth.openai.com
- agent_name: name shown during dynamic registration (default "evmbench")
- ext_agent_host_id: optional override for the generated per-deployment host ID
- plan_usage: "true" to request ChatGPT plan scopes in registered mode (dynamic mode always requests them)
"""

import secrets
from collections.abc import Mapping
from dataclasses import replace

from loguru import logger

from api.core.tokens import Token
from api.siwc.oauth import (
    DYNAMIC_CLIENT_ID,
    IDENTITY_SCOPES,
    PLAN_USAGE_SCOPES,
    IdTokenError,
    OAuthError,
    PkcePair,
    oauth_client_from_args,
    parse_scopes,
)

from .abc import AuthBackendABC, AuthContext, AuthorizationRequest, AuthResult, ChatGPTSignIn


USER_ID_PREFIX = 'chatgpt:'


def _truthy(value: str | None) -> bool:
    return (value or '').strip().lower() in {'1', 'true', 'yes', 'on'}


class ChatGPTAuthBackend(AuthBackendABC):
    provider = 'chatgpt'

    def __init__(self, args: dict[str, str]) -> None:
        super().__init__(args)
        self.oauth = oauth_client_from_args(args)
        self._agent_name = (args.get('agent_name') or 'evmbench').strip()
        self.ext_agent_host_id_override = (args.get('ext_agent_host_id') or '').strip() or None
        self._plan_usage = self.oauth.is_dynamic or _truthy(args.get('plan_usage', 'true'))

    @property
    def scopes(self) -> tuple[str, ...]:
        return IDENTITY_SCOPES + PLAN_USAGE_SCOPES if self._plan_usage else IDENTITY_SCOPES

    async def begin(self, context: AuthContext) -> AuthorizationRequest:
        pkce = PkcePair.generate()
        nonce = secrets.token_urlsafe(32)
        hint = context.account_hint

        if self.oauth.client_id:
            client_id = self.oauth.client_id
            agent_name_hint = None
        elif hint is not None:
            # Returning account: reuse its issued client ID; agent_name_hint is for first registration only.
            client_id = hint.client_id
            agent_name_hint = None
        else:
            client_id = DYNAMIC_CLIENT_ID
            agent_name_hint = self._agent_name

        url = await self.oauth.authorization_url(
            redirect_uri=context.redirect_uri,
            state=context.state,
            nonce=nonce,
            code_challenge=pkce.challenge,
            scopes=self.scopes,
            client_id=client_id,
            ext_agent_host_id=context.ext_agent_host_id if self.oauth.is_dynamic else None,
            agent_name_hint=agent_name_hint,
            id_token_hint=hint.id_token if hint is not None else None,
        )
        transaction = {
            'code_verifier': pkce.verifier,
            'nonce': nonce,
            'client_id': client_id,
            'ext_agent_host_id': context.ext_agent_host_id or '',
        }
        return AuthorizationRequest(url=url, transaction=transaction)

    def _issued_client_id(self, params: Mapping[str, str], transaction: Mapping[str, str]) -> str | None:
        if self.oauth.client_id:
            return self.oauth.client_id
        requested = transaction.get('client_id') or ''
        # A new dynamic registration returns the issued `oaiapp_...` ID on the callback.
        issued = (params.get('client_id') or '').strip()
        if requested == DYNAMIC_CLIENT_ID:
            return issued or None
        return requested or None

    async def complete(
        self,
        *,
        code: str,
        params: Mapping[str, str],
        transaction: Mapping[str, str],
        redirect_uri: str,
    ) -> AuthResult | None:
        if params.get('error'):
            logger.info(f'ChatGPT sign-in returned error={params.get("error")!r}')
            return None

        verifier = transaction.get('code_verifier')
        nonce = transaction.get('nonce')
        client_id = self._issued_client_id(params, transaction)
        if not verifier or not nonce or not client_id:
            logger.warning('ChatGPT sign-in callback is missing transaction state or the issued client_id')
            return None

        try:
            tokens = await self.oauth.exchange_code(
                code=code,
                code_verifier=verifier,
                redirect_uri=redirect_uri,
                client_id=client_id,
                include_resource=self._plan_usage,
            )
        except OAuthError as err:
            logger.warning(f'ChatGPT code exchange failed: {err.code}')
            return None

        if not tokens.id_token:
            logger.warning('ChatGPT token response did not include an ID token')
            return None
        try:
            claims = await self.oauth.verify_id_token(tokens.id_token, audience=client_id, nonce=nonce)
        except IdTokenError as err:
            logger.warning(f'ChatGPT ID token rejected: {err}')
            return None

        if not tokens.scopes:
            # Fall back to the scopes echoed on the callback.
            tokens = replace(tokens, scopes=parse_scopes(params.get('scope')))

        token = Token(
            user_id=f'{USER_ID_PREFIX}{claims.subject}',
            login=claims.name or claims.email or 'ChatGPT user',
            avatar_url=claims.picture,
            provider=self.provider,
        )
        return AuthResult(
            token=token,
            chatgpt=ChatGPTSignIn(
                subject=claims.subject,
                email=claims.email,
                client_id=client_id,
                ext_agent_host_id=transaction.get('ext_agent_host_id') or None,
                tokens=tokens,
            ),
        )
