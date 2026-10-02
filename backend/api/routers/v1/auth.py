import secrets
from datetime import UTC, datetime, timedelta
from http import HTTPStatus
from secrets import compare_digest
from typing import Annotated

import jwt
import orjson
from fastapi import APIRouter, Cookie, Depends, HTTPException, Request
from fastapi.responses import RedirectResponse
from loguru import logger
from sqlalchemy.ext.asyncio import AsyncSession

from api.auth.abc import AccountHint, AuthContext
from api.core.config import settings
from api.core.deps import TokenDep, get_db
from api.core.impl import auth_backend, chatgpt_auth
from api.core.tokens import decode_token, encode_token
from api.models.chatgpt_credential import ChatGPTCredential
from api.schemas.auth import UserObject
from api.siwc.credentials import PlanUsageState, get_ext_agent_host_id
from api.siwc.oauth import OAuthError
from api.util.aes_gcm import decrypt_token, derive_key, encrypt_token


router = APIRouter(prefix='/auth', tags=['auth'])
TRANSACTION_COOKIE_NAME = 'oauth_tx'
TRANSACTION_TTL_SECONDS = 10 * 60
# Remembers which ChatGPT account signed in last so re-sign-in reuses its issued client ID.
ACCOUNT_HINT_COOKIE_NAME = 'chatgpt_account'
ACCOUNT_HINT_TTL_SECONDS = 365 * 24 * 60 * 60

DbSessionDep = Annotated[AsyncSession, Depends(get_db)]


def _cookie_key(context: str) -> bytes:
    return derive_key(settings.BACKEND_JWT_SECRET.get_secret_value() + context)


def _encode_transaction(transaction: dict[str, str]) -> str:
    payload = orjson.dumps({**transaction, 'iat': int(datetime.now(tz=UTC).timestamp())}).decode()
    return encrypt_token(payload, key=_cookie_key(':oauth-transaction'))


def _decode_transaction(value: str | None) -> dict[str, str] | None:
    if not value:
        return None
    try:
        payload = orjson.loads(decrypt_token(value, key=_cookie_key(':oauth-transaction')))
    except (ValueError, orjson.JSONDecodeError):
        return None
    if not isinstance(payload, dict):
        return None
    issued_at = payload.pop('iat', 0)
    if not isinstance(issued_at, int) or datetime.now(tz=UTC).timestamp() - issued_at > TRANSACTION_TTL_SECONDS:
        return None
    return {str(k): str(v) for k, v in payload.items()}


def _encode_account_hint(user_id: str) -> str:
    exp = datetime.now(tz=UTC) + timedelta(seconds=ACCOUNT_HINT_TTL_SECONDS)
    return jwt.encode(
        {'uid': user_id, 'exp': int(exp.timestamp()), 'typ': 'chatgpt_account'},
        settings.BACKEND_JWT_SECRET.get_secret_value(),
        algorithm='HS256',
    )


def _decode_account_hint(value: str | None) -> str | None:
    if not value:
        return None
    try:
        payload = jwt.decode(
            value,
            settings.BACKEND_JWT_SECRET.get_secret_value(),
            algorithms=['HS256'],
            options={'require': ['exp']},
        )
    except jwt.PyJWTError:
        return None
    if payload.get('typ') != 'chatgpt_account' or not isinstance(payload.get('uid'), str):
        return None
    return payload['uid']


def _callback_url() -> str:
    return settings.BACKEND_PUBLIC_URL.rstrip('/') + '/v1/auth/callback'


@router.get('/')
async def redirect_to_auth(
    session: DbSessionDep,
    switch_account: bool = False,  # noqa: FBT001, FBT002
    account_hint: Annotated[str | None, Cookie(alias=ACCOUNT_HINT_COOKIE_NAME)] = None,
) -> RedirectResponse:
    if not auth_backend:
        raise HTTPException(status_code=HTTPStatus.NOT_FOUND, detail='Not found')

    hint: AccountHint | None = None
    ext_agent_host_id: str | None = None
    chatgpt = chatgpt_auth()
    if chatgpt is not None:
        backend, store = chatgpt
        if backend.oauth.is_dynamic:
            ext_agent_host_id = await get_ext_agent_host_id(session, override=backend.ext_agent_host_id_override)
        hinted_user_id = None if switch_account else _decode_account_hint(account_hint)
        if hinted_user_id:
            credential = await session.get(ChatGPTCredential, hinted_user_id)
            id_token = store.id_token(credential) if credential else None
            if credential is not None and id_token:
                hint = AccountHint(client_id=credential.client_id, id_token=id_token)

    state = secrets.token_urlsafe(32)
    request = await auth_backend.begin(
        AuthContext(
            redirect_uri=_callback_url(),
            state=state,
            account_hint=hint,
            ext_agent_host_id=ext_agent_host_id,
        ),
    )
    redirect = RedirectResponse(url=request.url, status_code=HTTPStatus.TEMPORARY_REDIRECT)
    redirect.set_cookie(
        key=TRANSACTION_COOKIE_NAME,
        value=_encode_transaction({**request.transaction, 'state': state}),
        httponly=True,
        samesite='lax',
        secure=not settings.BACKEND_DEV,
        max_age=TRANSACTION_TTL_SECONDS,
        expires=datetime.now(tz=UTC) + timedelta(seconds=TRANSACTION_TTL_SECONDS),
    )
    if switch_account:
        redirect.delete_cookie(key=ACCOUNT_HINT_COOKIE_NAME, samesite='lax')
    return redirect


@router.get('/callback')
async def auth_callback(
    request: Request,
    session: DbSessionDep,
    code: str | None = None,
    state: str | None = None,
    oauth_tx: Annotated[str | None, Cookie(alias=TRANSACTION_COOKIE_NAME)] = None,
) -> RedirectResponse:
    if not auth_backend:
        raise HTTPException(status_code=HTTPStatus.NOT_FOUND, detail='Not found')

    redirect = RedirectResponse(
        url=settings.FRONTEND_PUBLIC_URL,
        status_code=HTTPStatus.TEMPORARY_REDIRECT,
    )
    redirect.delete_cookie(
        key=TRANSACTION_COOKIE_NAME,
        samesite='lax',
    )

    # Transactions are single use: the cookie is cleared above whatever happens next.
    transaction = _decode_transaction(oauth_tx)
    expected_state = transaction.get('state') if transaction else None
    if not code or not state or not expected_state or not compare_digest(state, expected_state):
        return redirect

    result = await auth_backend.complete(
        code=code,
        params=dict(request.query_params),
        transaction=transaction or {},
        redirect_uri=_callback_url(),
    )
    if not result:
        return redirect

    if result.chatgpt is not None:
        chatgpt = chatgpt_auth()
        if chatgpt is None:
            return redirect
        _, store = chatgpt
        await store.save(
            session,
            user_id=result.token.user_id,
            subject=result.chatgpt.subject,
            email=result.chatgpt.email,
            client_id=result.chatgpt.client_id,
            ext_agent_host_id=result.chatgpt.ext_agent_host_id,
            tokens=result.chatgpt.tokens,
        )
        redirect.set_cookie(
            key=ACCOUNT_HINT_COOKIE_NAME,
            value=_encode_account_hint(result.token.user_id),
            httponly=True,
            samesite='lax',
            secure=not settings.BACKEND_DEV,
            max_age=ACCOUNT_HINT_TTL_SECONDS,
        )

    ttl_seconds = settings.BACKEND_JWT_TTL_SECONDS
    redirect.set_cookie(
        key='session',
        value=encode_token(result.token),
        httponly=True,
        samesite='lax',
        max_age=ttl_seconds,
        expires=datetime.now(tz=UTC) + timedelta(seconds=ttl_seconds),
    )
    return redirect


@router.get('/me')
async def get_me(token: TokenDep, session: DbSessionDep) -> UserObject:
    plan_usage: PlanUsageState = 'unavailable'
    chatgpt = chatgpt_auth()
    if token.provider == 'chatgpt' and chatgpt is not None and settings.plan_usage_enabled:
        _, store = chatgpt
        credential = await session.get(ChatGPTCredential, token.user_id)
        plan_usage = store.plan_usage_status(credential).state

    return UserObject(
        avatar_url=token.avatar_url,
        username=token.login,
        provider=token.provider,
        plan_usage=plan_usage,
    )


@router.get('/logout')
async def logout(
    session: DbSessionDep,
    session_cookie: Annotated[str | None, Cookie(alias='session')] = None,
) -> RedirectResponse:
    if not auth_backend:
        raise HTTPException(status_code=HTTPStatus.NOT_FOUND, detail='Not found')

    chatgpt = chatgpt_auth()
    if chatgpt is not None and session_cookie:
        token = decode_token(session_cookie)
        if token is not None and token.provider == 'chatgpt':
            backend, store = chatgpt
            try:
                await store.sign_out(session, user_id=token.user_id, oauth=backend.oauth)
            except OAuthError as err:
                logger.warning(f'ChatGPT sign-out revocation failed: {err.code}')

    redirect = RedirectResponse(
        url=settings.FRONTEND_PUBLIC_URL,
        status_code=HTTPStatus.TEMPORARY_REDIRECT,
    )
    redirect.delete_cookie(
        key='session',
        samesite='lax',
        secure=not settings.BACKEND_DEV,
    )
    return redirect
