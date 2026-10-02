"""Opaque job-bound tokens handed to workers instead of any OpenAI credential.

The worker sends the token to oai_proxy as its bearer credential; the proxy decrypts it, checks the job,
and injects the user's ChatGPT access token. Tokens are useless outside the proxy and expire with the job.
"""

import time
from dataclasses import dataclass
from uuid import UUID

import orjson

from api.util.aes_gcm import decrypt_token, derive_key, encrypt_token


JOB_TOKEN_PREFIX = 'siwc.'  # noqa: S105 - not a secret, a token type marker
_KEY_CONTEXT = ':siwc-job-token'


@dataclass(frozen=True)
class JobTokenClaims:
    user_id: str
    job_id: UUID
    # Resolved upstream model slug; the proxy rejects requests for any other model.
    model: str
    exp: int


def _key(secret: str) -> bytes:
    return derive_key(secret + _KEY_CONTEXT)


def issue_job_token(claims: JobTokenClaims, *, secret: str) -> str:
    payload = orjson.dumps(
        {'uid': claims.user_id, 'jid': str(claims.job_id), 'model': claims.model, 'exp': claims.exp},
    ).decode()
    return JOB_TOKEN_PREFIX + encrypt_token(payload, key=_key(secret))


def is_job_token(token: str) -> bool:
    return token.startswith(JOB_TOKEN_PREFIX)


def parse_job_token(token: str, *, secret: str, now: float | None = None) -> JobTokenClaims | None:
    if not is_job_token(token):
        return None
    try:
        payload = orjson.loads(decrypt_token(token.removeprefix(JOB_TOKEN_PREFIX), key=_key(secret)))
        claims = JobTokenClaims(
            user_id=str(payload['uid']),
            job_id=UUID(payload['jid']),
            model=str(payload['model']),
            exp=int(payload['exp']),
        )
    except (ValueError, KeyError, TypeError, orjson.JSONDecodeError):
        return None
    if claims.exp <= (time.time() if now is None else now):
        return None
    return claims
