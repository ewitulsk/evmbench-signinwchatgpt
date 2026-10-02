import re


SUBSCRIPTION_SHARING_PREFIX = 'subscription_sharing_'
_SUBSCRIPTION_SHARING_RE = re.compile(rb'subscription_sharing_[a-z_]+')
# Longest code we look for; used to keep a rolling buffer when scanning streamed chunks.
MAX_CODE_LENGTH = 96

# Job error codes surfaced to the frontend.
REAUTH_REQUIRED = 'reauth_required'
MODEL_UNAVAILABLE = 'model_unavailable'
PLAN_USAGE_UNAVAILABLE = 'plan_usage_unavailable'

# OAuth token errors that mean the stored credential can never work again.
UNUSABLE_TOKEN_ERRORS = frozenset({'invalid_grant', 'token_expired', 'refresh_token_reused'})


def normalize_error_code(code: str) -> str:
    """Map `subscription_sharing_usage_limit_exceeded` to `usage_limit_exceeded`."""
    return code.removeprefix(SUBSCRIPTION_SHARING_PREFIX)


def find_error_code(data: bytes) -> str | None:
    """Find the first subscription-sharing error code in a response body or SSE chunk."""
    match = _SUBSCRIPTION_SHARING_RE.search(data)
    if match is None:
        return None
    return normalize_error_code(match.group().decode('ascii'))
