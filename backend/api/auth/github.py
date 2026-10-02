from collections.abc import Mapping
from http import HTTPStatus
from urllib.parse import quote, urlencode

import orjson
from httpx import AsyncClient, Response

from api.core.tokens import Token

from .abc import AuthBackendABC, AuthContext, AuthorizationRequest, AuthResult


def get_json(in_response: Response) -> dict[str, str] | None:
    if in_response.status_code != HTTPStatus.OK:
        return None

    try:
        out_response = orjson.loads(in_response.text)
    except orjson.JSONDecodeError:
        return None

    if not isinstance(out_response, dict):
        return None

    return out_response


class GithubAuthBackend(AuthBackendABC):
    provider = 'github'

    def __init__(self, args: dict[str, str]) -> None:
        super().__init__(args)
        self._client_id = self._args.get('client_id')
        self._client_secret = self._args.get('client_secret')

        if not self._client_id or not self._client_secret:
            msg = 'Client id or secret is not set'
            raise ValueError(msg)

    async def begin(self, context: AuthContext) -> AuthorizationRequest:
        qs = urlencode(
            query={
                'client_id': self._client_id,
                'redirect_uri': context.redirect_uri,
                'state': context.state,
                # no scopes are needed
            },
            quote_via=quote,
        )
        return AuthorizationRequest(url=f'https://github.com/login/oauth/authorize?{qs}')

    async def complete(
        self,
        *,
        code: str,
        params: Mapping[str, str],  # noqa: ARG002
        transaction: Mapping[str, str],  # noqa: ARG002
        redirect_uri: str,  # noqa: ARG002
    ) -> AuthResult | None:
        token = await self.get_token(code)
        return AuthResult(token=token) if token else None

    async def get_token(self, code: str) -> Token | None:
        async with AsyncClient() as client:
            r = get_json(
                await client.post(
                    'https://github.com/login/oauth/access_token',
                    headers={
                        'Accept': 'application/json',
                    },
                    json={
                        'client_id': self._client_id,
                        'client_secret': self._client_secret,
                        'code': code,
                    },
                )
            )
            if not r:
                return None

            r = get_json(
                await client.get(
                    'https://api.github.com/user',
                    headers={
                        'Accept': 'application/json',
                        'Authorization': f'Bearer {r.get("access_token")}',
                    },
                )
            )
            if not r:
                return None

            return Token(
                user_id=str(r['id']),
                login=r['login'],
                avatar_url=r.get('avatar_url'),
                provider='github',
            )
