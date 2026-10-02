"""ChatGPT-plan (Sign in with ChatGPT) token broker.

Workers authenticate with a job-bound token (api/siwc/job_token.py). For each request the proxy checks the job,
pins the model, strips parameters the preview rejects, swaps in the user's ChatGPT access token (refreshing it
when needed), and records subscription-sharing errors on the job so the UI can explain them.
"""

from collections.abc import AsyncIterator
from functools import lru_cache
from typing import Any

import httpx
import orjson
from fastapi import Request
from fastapi.responses import ORJSONResponse, Response
from loguru import logger
from starlette.background import BackgroundTask
from starlette.responses import StreamingResponse

from api.core.database import DatabaseManager
from api.models.job import Job, JobStatus
from api.siwc.credentials import (
    CredentialStore,
    PlanUsageUnavailableError,
    ReauthRequiredError,
    record_job_error_code,
)
from api.siwc.errors import MAX_CODE_LENGTH, PLAN_USAGE_UNAVAILABLE, REAUTH_REQUIRED, find_error_code
from api.siwc.job_token import JobTokenClaims, parse_job_token
from api.siwc.oauth import OAuthError, SiwcOAuthClient, oauth_client_from_args
from oai_proxy.core.config import settings
from oai_proxy.core.headers import filter_headers


OPENAI_BASE_URL = 'https://api.openai.com'
ALLOWED_ROUTES = frozenset({('POST', 'v1/responses'), ('GET', 'v1/models')})
ACTIVE_JOB_STATUSES = (JobStatus.queued, JobStatus.running)

# Responses parameters the ChatGPT-plan preview rejects (see the SIWC "Preview limitations" page).
UNSUPPORTED_PARAMS = ('background', 'conversation', 'max_output_tokens', 'temperature', 'top_p')
UNSUPPORTED_TOOL_TYPES = frozenset(
    {
        'code_interpreter',
        'computer_use',
        'computer_use_preview',
        'file_search',
        'image_generation',
        'mcp',
        'tool_search',
        'web_search',
        'web_search_preview',
    },
)


@lru_cache(maxsize=1)
def _db() -> DatabaseManager:
    if settings.DATABASE_DSN is None:
        msg = 'DATABASE_DSN must be set for ChatGPT plan usage'
        raise RuntimeError(msg)
    return DatabaseManager(
        database_url=str(settings.DATABASE_DSN.get_secret_value()),
        pool_size=settings.OAI_PROXY_DATABASE_POOL_SIZE,
    )


@lru_cache(maxsize=1)
def _oauth() -> SiwcOAuthClient:
    return oauth_client_from_args(settings.AUTH_BACKEND_ARGUMENTS)


@lru_cache(maxsize=1)
def _store() -> CredentialStore:
    if settings.CREDENTIALS_AES_KEY is None:
        msg = 'CREDENTIALS_AES_KEY must be set for ChatGPT plan usage'
        raise RuntimeError(msg)
    return CredentialStore(settings.CREDENTIALS_AES_KEY.get_secret_value())


def _error(status_code: int, code: str, message: str) -> ORJSONResponse:
    # OpenAI-style error body so Codex surfaces the message.
    return ORJSONResponse(
        status_code=status_code,
        content={'error': {'code': code, 'message': message, 'type': 'invalid_request_error'}},
    )


def sanitize_responses_body(body: dict[str, Any]) -> dict[str, Any]:
    """Make a Responses request acceptable to the ChatGPT-plan preview."""
    sanitized = {key: value for key, value in body.items() if key not in UNSUPPORTED_PARAMS}
    sanitized['store'] = False
    sanitized['stream'] = True

    tools = sanitized.get('tools')
    if isinstance(tools, list):
        sanitized['tools'] = [
            tool for tool in tools if not (isinstance(tool, dict) and tool.get('type') in UNSUPPORTED_TOOL_TYPES)
        ]

    # Explicit system messages are rejected; developer messages carry the same instructions.
    items = sanitized.get('input')
    if isinstance(items, list):
        sanitized['input'] = [
            {**item, 'role': 'developer'}
            if isinstance(item, dict) and item.get('role') == 'system' and item.get('type', 'message') == 'message'
            else item
            for item in items
        ]
    return sanitized


async def _record_error_code(claims: JobTokenClaims, code: str) -> None:
    try:
        async with _db().acquire() as session:
            await record_job_error_code(session, job_id=claims.job_id, code=code)
    except Exception as err:  # noqa: BLE001
        logger.opt(exception=err).warning(f'Unable to record error_code={code} for job_id={claims.job_id}')


async def _scan_stream(chunks: AsyncIterator[bytes], claims: JobTokenClaims) -> AsyncIterator[bytes]:
    """Pass the upstream stream through unchanged, noting the first subscription-sharing error code."""
    tail = b''
    found = False
    async for chunk in chunks:
        if not found:
            window = tail + chunk
            code = find_error_code(window)
            if code is not None:
                found = True
                logger.info(f'ChatGPT plan error code={code} for job_id={claims.job_id}')
                await _record_error_code(claims, code)
            tail = window[-MAX_CODE_LENGTH:]
        yield chunk


async def _load_access_token(claims: JobTokenClaims) -> str | ORJSONResponse:
    async with _db().acquire() as session:
        job = await session.get(Job, claims.job_id)
        if (
            job is None
            or job.user_id != claims.user_id
            or job.billing_source != 'chatgpt_plan'
            or job.status not in ACTIVE_JOB_STATUSES
        ):
            return _error(401, 'job_not_active', 'This job is no longer active')

        try:
            return await _store().get_access_token(session, user_id=claims.user_id, oauth=_oauth())
        except ReauthRequiredError:
            await record_job_error_code(session, job_id=claims.job_id, code=REAUTH_REQUIRED)
            return _error(401, REAUTH_REQUIRED, 'Reconnect ChatGPT in EVM Bench to continue using your plan')
        except PlanUsageUnavailableError:
            await record_job_error_code(session, job_id=claims.job_id, code=PLAN_USAGE_UNAVAILABLE)
            return _error(403, PLAN_USAGE_UNAVAILABLE, 'ChatGPT plan usage is not enabled for this account')
        except OAuthError as err:
            # Temporary auth-server trouble: the credential is kept, and Codex retries.
            logger.warning(f'ChatGPT token refresh failed for job_id={claims.job_id}: {err.code}')
            return _error(503, 'token_refresh_unavailable', 'Unable to refresh ChatGPT credentials; retry shortly')


async def _prepare_body(request: Request, claims: JobTokenClaims) -> bytes | ORJSONResponse:
    encoding = (request.headers.get('content-encoding') or 'identity').lower()
    if encoding != 'identity':
        return _error(415, 'unsupported_encoding', 'Compressed request bodies are not supported')
    try:
        body = orjson.loads(await request.body())
    except orjson.JSONDecodeError:
        return _error(400, 'invalid_json', 'Request body must be JSON')
    if not isinstance(body, dict):
        return _error(400, 'invalid_json', 'Request body must be a JSON object')
    if body.get('model') != claims.model:
        # Jobs are pinned to the model the user picked, so a compromised worker can't spend the plan elsewhere.
        return _error(403, 'model_not_allowed', f'This job may only use {claims.model}')
    return orjson.dumps(sanitize_responses_body(body))


async def proxy_siwc_request(  # noqa: PLR0911 - one early return per rejection reason
    request: Request,
    path: str,
    token: str,
    forward_headers: dict[str, str],
) -> Response:
    if not settings.siwc_enabled or settings.OAI_PROXY_AES_KEY is None:
        return _error(501, 'siwc_not_configured', 'ChatGPT plan usage is not configured on this proxy')

    claims = parse_job_token(token, secret=settings.OAI_PROXY_AES_KEY.get_secret_value())
    if claims is None:
        return _error(401, 'invalid_token', 'Invalid or expired job token')

    route = path.strip('/')
    method = request.method.upper()
    if (method, route) not in ALLOWED_ROUTES:
        return _error(403, 'route_not_allowed', f'{method} /{route} is not available for ChatGPT plan jobs')

    content: bytes | None = None
    if method == 'POST':
        prepared = await _prepare_body(request, claims)
        if isinstance(prepared, Response):
            return prepared
        content = prepared
        forward_headers['content-type'] = 'application/json'
        forward_headers.pop('content-encoding', None)

    access_token = await _load_access_token(claims)
    if isinstance(access_token, Response):
        return access_token
    forward_headers['authorization'] = f'Bearer {access_token}'

    client = httpx.AsyncClient(timeout=httpx.Timeout(60.0, read=None))
    try:
        upstream = await client.send(
            client.build_request(
                method,
                f'{OPENAI_BASE_URL}/{route}',
                params=request.query_params,
                headers=forward_headers,
                content=content,
            ),
            stream=True,
        )
    except httpx.HTTPError as err:
        await client.aclose()
        logger.warning(f'Upstream request failed for job_id={claims.job_id}: {type(err).__name__}')
        return _error(502, 'upstream_unavailable', 'Unable to reach OpenAI')

    async def _cleanup() -> None:
        await upstream.aclose()
        await client.aclose()

    return StreamingResponse(
        _scan_stream(upstream.aiter_raw(), claims),
        status_code=upstream.status_code,
        headers=filter_headers(upstream.headers.items()),
        background=BackgroundTask(_cleanup),
    )
