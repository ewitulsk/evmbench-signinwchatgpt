"""OAuth 2.0 / OpenID Connect client for Sign in with ChatGPT.

Docs: https://developers.openai.com/siwc
"""

import base64
import hashlib
import secrets
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime, timedelta
from urllib.parse import quote, urlencode

import httpx
import jwt
import orjson
from loguru import logger


DEFAULT_ISSUER = 'https://auth.openai.com'
API_RESOURCE = 'https://api.openai.com/v1'
DYNAMIC_CLIENT_ID = 'dynamic_agent_client'
PLAN_USAGE_SCOPE = 'chatgpt.tokens.use.direct'
IDENTITY_SCOPES = ('openid', 'profile', 'email')
PLAN_USAGE_SCOPES = ('offline_access', 'resource.invoke', PLAN_USAGE_SCOPE)

_DISCOVERY_TTL_SECONDS = 60 * 60
_JWKS_TTL_SECONDS = 60 * 60
_HTTP_TIMEOUT = httpx.Timeout(15.0)

HttpClientFactory = Callable[[], httpx.AsyncClient]


class OAuthError(Exception):
    def __init__(self, code: str, description: str = '', *, status_code: int | None = None) -> None:
        super().__init__(f'{code}: {description}' if description else code)
        self.code = code
        self.description = description
        self.status_code = status_code


class TemporaryOAuthError(OAuthError):
    """Network failure or 5xx from the authorization server; the credential is still good."""


class IdTokenError(Exception): ...


@dataclass(frozen=True)
class OIDCConfig:
    issuer: str
    authorization_endpoint: str
    token_endpoint: str
    jwks_uri: str
    revocation_endpoint: str | None = None

    @classmethod
    def defaults(cls, issuer: str) -> 'OIDCConfig':
        return cls(
            issuer=issuer,
            authorization_endpoint=f'{issuer}/api/accounts/authorize',
            token_endpoint=f'{issuer}/api/accounts/oauth/token',
            jwks_uri=f'{issuer}/.well-known/jwks.json',
        )


@dataclass(frozen=True)
class TokenSet:
    access_token: str
    expires_at: datetime
    scopes: tuple[str, ...]
    refresh_token: str | None = None
    id_token: str | None = None
    earliest_refresh_at: datetime | None = None
    client_id: str | None = None

    @property
    def has_plan_usage(self) -> bool:
        return PLAN_USAGE_SCOPE in self.scopes


@dataclass(frozen=True)
class IdTokenClaims:
    subject: str
    audience: str
    email: str | None = None
    name: str | None = None
    picture: str | None = None


@dataclass(frozen=True)
class PkcePair:
    verifier: str
    challenge: str

    @classmethod
    def generate(cls) -> 'PkcePair':
        verifier = secrets.token_urlsafe(64)
        digest = hashlib.sha256(verifier.encode('ascii')).digest()
        challenge = base64.urlsafe_b64encode(digest).decode('ascii').rstrip('=')
        return cls(verifier=verifier, challenge=challenge)


def parse_scopes(value: object) -> tuple[str, ...]:
    if isinstance(value, str):
        return tuple(sorted({item for item in value.replace('+', ' ').split() if item}))
    if isinstance(value, list):
        return tuple(sorted({item for item in value if isinstance(item, str) and item}))
    return ()


def _parse_timestamp(value: object) -> datetime | None:
    """`earliest_refresh_at` format isn't documented; accept epoch seconds or ISO 8601."""
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, int | float):
        return datetime.fromtimestamp(value, tz=UTC)
    if isinstance(value, str) and value.strip():
        text = value.strip()
        try:
            return datetime.fromtimestamp(float(text), tz=UTC)
        except ValueError:
            pass
        try:
            parsed = datetime.fromisoformat(text)
        except ValueError:
            return None
        return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)
    return None


def _parse_token_response(payload: Mapping[str, object], *, now: datetime) -> TokenSet:
    access_token = payload.get('access_token')
    if not isinstance(access_token, str) or not access_token:
        msg = 'missing_access_token'
        raise OAuthError(msg, 'Token response did not include an access token')

    expires_in = payload.get('expires_in')
    if isinstance(expires_in, bool) or not isinstance(expires_in, int | float) or expires_in <= 0:
        expires_in = 3600

    refresh_token = payload.get('refresh_token')
    id_token = payload.get('id_token')
    client_id = payload.get('client_id')
    return TokenSet(
        access_token=access_token,
        expires_at=now + timedelta(seconds=float(expires_in)),
        scopes=parse_scopes(payload.get('scope')),
        refresh_token=refresh_token if isinstance(refresh_token, str) and refresh_token else None,
        id_token=id_token if isinstance(id_token, str) and id_token else None,
        earliest_refresh_at=_parse_timestamp(payload.get('earliest_refresh_at')),
        client_id=client_id if isinstance(client_id, str) and client_id else None,
    )


def _error_from_response(response: httpx.Response) -> OAuthError:
    code = f'http_{response.status_code}'
    description = ''
    try:
        payload = orjson.loads(response.content)
    except orjson.JSONDecodeError:
        payload = None
    if isinstance(payload, dict):
        error = payload.get('error')
        if isinstance(error, dict):
            code = str(error.get('code') or error.get('type') or code)
            description = str(error.get('message') or '')
        elif isinstance(error, str) and error:
            code = error
            description = str(payload.get('error_description') or '')
        elif isinstance(payload.get('detail'), str):
            description = payload['detail']

    cls = TemporaryOAuthError if response.status_code >= 500 else OAuthError  # noqa: PLR2004
    return cls(code, description, status_code=response.status_code)


@dataclass
class SiwcOAuthClient:
    """Talks to the OpenAI authorization server.

    `client_id` / `client_secret` are only set for a registered (commercial) client. Open-source deployments
    use dynamic registration: each ChatGPT account gets its own issued `oaiapp_...` client ID.
    """

    issuer: str = DEFAULT_ISSUER
    client_id: str | None = None
    client_secret: str | None = None
    http_client_factory: HttpClientFactory = field(
        default=lambda: httpx.AsyncClient(timeout=_HTTP_TIMEOUT),
    )

    _config: OIDCConfig | None = field(default=None, init=False, repr=False)
    _config_fetched_at: float = field(default=0.0, init=False, repr=False)
    _jwks: jwt.PyJWKSet | None = field(default=None, init=False, repr=False)
    _jwks_fetched_at: float = field(default=0.0, init=False, repr=False)

    def __post_init__(self) -> None:
        self.issuer = self.issuer.rstrip('/')

    @property
    def is_dynamic(self) -> bool:
        return not self.client_id

    async def config(self) -> OIDCConfig:
        if self._config is not None and time.monotonic() - self._config_fetched_at < _DISCOVERY_TTL_SECONDS:
            return self._config

        config = OIDCConfig.defaults(self.issuer)
        try:
            async with self.http_client_factory() as client:
                response = await client.get(f'{self.issuer}/.well-known/openid-configuration')
            if response.status_code == httpx.codes.OK:
                data = orjson.loads(response.content)
                config = replace(
                    config,
                    authorization_endpoint=data.get('authorization_endpoint') or config.authorization_endpoint,
                    token_endpoint=data.get('token_endpoint') or config.token_endpoint,
                    jwks_uri=data.get('jwks_uri') or config.jwks_uri,
                    revocation_endpoint=data.get('revocation_endpoint') or config.revocation_endpoint,
                )
            else:
                logger.warning(f'OIDC discovery returned {response.status_code}; using documented endpoints')
        except (httpx.HTTPError, orjson.JSONDecodeError, AttributeError) as err:
            logger.warning(f'OIDC discovery failed ({type(err).__name__}); using documented endpoints')

        self._config = config
        self._config_fetched_at = time.monotonic()
        return config

    async def authorization_url(  # noqa: PLR0913
        self,
        *,
        redirect_uri: str,
        state: str,
        nonce: str,
        code_challenge: str,
        scopes: tuple[str, ...],
        client_id: str,
        ext_agent_host_id: str | None = None,
        agent_name_hint: str | None = None,
        id_token_hint: str | None = None,
    ) -> str:
        config = await self.config()
        query: dict[str, str] = {
            'client_id': client_id,
            'response_type': 'code',
            'redirect_uri': redirect_uri,
            'scope': ' '.join(scopes),
            'state': state,
            'nonce': nonce,
            'code_challenge': code_challenge,
            'code_challenge_method': 'S256',
        }
        if PLAN_USAGE_SCOPE in scopes:
            query['resource'] = API_RESOURCE
        if ext_agent_host_id:
            query['ext_agent_host_id'] = ext_agent_host_id
        if agent_name_hint:
            query['agent_name_hint'] = agent_name_hint
        if id_token_hint:
            query['id_token_hint'] = id_token_hint
        return f'{config.authorization_endpoint}?{urlencode(query, quote_via=quote)}'

    def _client_auth(self, client_id: str, data: dict[str, str]) -> httpx.BasicAuth | None:
        if self.client_secret and client_id == self.client_id:
            # Confidential client: secret in the Authorization header, never the body.
            return httpx.BasicAuth(quote(client_id, safe=''), quote(self.client_secret, safe=''))
        data['client_id'] = client_id
        return None

    async def _token_request(self, data: dict[str, str], *, client_id: str) -> TokenSet:
        config = await self.config()
        auth = self._client_auth(client_id, data)
        try:
            async with self.http_client_factory() as client:
                response = await client.post(
                    config.token_endpoint,
                    data=data,
                    headers={'Accept': 'application/json'},
                    auth=auth or httpx.USE_CLIENT_DEFAULT,
                )
        except httpx.HTTPError as err:
            msg = 'network_error'
            raise TemporaryOAuthError(msg, type(err).__name__) from err

        if response.status_code != httpx.codes.OK:
            raise _error_from_response(response)
        try:
            payload = orjson.loads(response.content)
        except orjson.JSONDecodeError as err:
            msg = 'invalid_token_response'
            raise TemporaryOAuthError(msg) from err
        if not isinstance(payload, dict):
            msg = 'invalid_token_response'
            raise TemporaryOAuthError(msg)
        return _parse_token_response(payload, now=datetime.now(tz=UTC))

    async def exchange_code(
        self,
        *,
        code: str,
        code_verifier: str,
        redirect_uri: str,
        client_id: str,
        include_resource: bool,
    ) -> TokenSet:
        data = {
            'grant_type': 'authorization_code',
            'code': code,
            'code_verifier': code_verifier,
            'redirect_uri': redirect_uri,
        }
        if include_resource:
            data['resource'] = API_RESOURCE
        return await self._token_request(data, client_id=client_id)

    async def refresh(self, *, client_id: str, refresh_token: str) -> TokenSet:
        # Omitting `scope` keeps the original grant.
        data = {'grant_type': 'refresh_token', 'refresh_token': refresh_token, 'resource': API_RESOURCE}
        return await self._token_request(data, client_id=client_id)

    async def revoke(self, *, client_id: str, refresh_token: str, attempts: int = 3) -> bool:
        config = await self.config()
        if not config.revocation_endpoint:
            logger.warning('No revocation_endpoint in OIDC discovery; skipping token revocation')
            return False

        data = {'token': refresh_token, 'token_type_hint': 'refresh_token'}
        auth = self._client_auth(client_id, data)
        for attempt in range(attempts):
            try:
                async with self.http_client_factory() as client:
                    response = await client.post(
                        config.revocation_endpoint,
                        data=data,
                        auth=auth or httpx.USE_CLIENT_DEFAULT,
                    )
                if response.status_code == httpx.codes.OK:
                    return True
                if response.status_code < 500:  # noqa: PLR2004
                    logger.warning(f'Token revocation rejected with {response.status_code}')
                    return False
            except httpx.HTTPError as err:
                logger.warning(f'Token revocation failed ({type(err).__name__}), attempt {attempt + 1}')
        return False

    async def _signing_keys(self, *, force: bool = False) -> jwt.PyJWKSet:
        if not force and self._jwks is not None and time.monotonic() - self._jwks_fetched_at < _JWKS_TTL_SECONDS:
            return self._jwks
        config = await self.config()
        async with self.http_client_factory() as client:
            response = await client.get(config.jwks_uri)
        response.raise_for_status()
        self._jwks = jwt.PyJWKSet.from_dict(orjson.loads(response.content))
        self._jwks_fetched_at = time.monotonic()
        return self._jwks

    async def verify_id_token(self, id_token: str, *, audience: str, nonce: str) -> IdTokenClaims:
        try:
            kid = jwt.get_unverified_header(id_token).get('kid')
        except jwt.PyJWTError as err:
            msg = 'Malformed ID token'
            raise IdTokenError(msg) from err

        try:
            keys = await self._signing_keys()
            key = next((k for k in keys.keys if k.key_id == kid), None)
            if key is None:
                # Key rotation: refetch once.
                keys = await self._signing_keys(force=True)
                key = next((k for k in keys.keys if k.key_id == kid), None)
        except (httpx.HTTPError, jwt.PyJWTError, orjson.JSONDecodeError) as err:
            msg = 'Unable to load OpenAI signing keys'
            raise IdTokenError(msg) from err
        if key is None:
            msg = 'ID token signed with an unknown key'
            raise IdTokenError(msg)

        config = await self.config()
        try:
            claims = jwt.decode(
                id_token,
                key=key,
                algorithms=[key.algorithm_name] if key.algorithm_name else ['RS256'],
                audience=audience,
                issuer=config.issuer,
                options={'require': ['exp', 'iat', 'sub', 'aud', 'iss']},
                leeway=60,
            )
        except jwt.PyJWTError as err:
            msg = f'Invalid ID token: {err}'
            raise IdTokenError(msg) from err

        if not secrets.compare_digest(str(claims.get('nonce') or ''), nonce):
            msg = 'ID token nonce mismatch'
            raise IdTokenError(msg)

        def _opt(name: str) -> str | None:
            value = claims.get(name)
            return value if isinstance(value, str) and value else None

        return IdTokenClaims(
            subject=str(claims['sub']),
            audience=audience,
            email=_opt('email'),
            name=_opt('name'),
            picture=_opt('picture'),
        )


def oauth_client_from_args(args: Mapping[str, str]) -> SiwcOAuthClient:
    """Build a client from AUTH_BACKEND_ARGUMENTS (shared by the API and oai_proxy)."""
    mode = (args.get('mode') or 'dynamic').strip().lower()
    if mode not in {'dynamic', 'registered'}:
        msg = f'Unknown chatgpt auth mode: {mode!r} (expected "dynamic" or "registered")'
        raise ValueError(msg)

    client_id = (args.get('client_id') or '').strip() or None
    client_secret = (args.get('client_secret') or '').strip() or None
    if mode == 'registered' and not client_id:
        msg = 'client_id is required for chatgpt auth mode "registered"'
        raise ValueError(msg)

    return SiwcOAuthClient(
        issuer=(args.get('issuer') or DEFAULT_ISSUER).strip(),
        client_id=client_id if mode == 'registered' else None,
        client_secret=client_secret if mode == 'registered' else None,
    )
