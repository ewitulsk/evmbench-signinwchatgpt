from abc import ABC, abstractmethod
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import ClassVar

from api.core.tokens import Token
from api.siwc.oauth import TokenSet


@dataclass(frozen=True)
class AccountHint:
    """A previously signed-in ChatGPT account, used to skip the account selector on re-sign-in."""

    client_id: str
    id_token: str


@dataclass(frozen=True)
class AuthContext:
    redirect_uri: str
    state: str
    account_hint: AccountHint | None = None
    ext_agent_host_id: str | None = None


@dataclass(frozen=True)
class AuthorizationRequest:
    url: str
    # Kept in an encrypted, short-lived cookie until the callback (PKCE verifier, nonce, ...).
    transaction: dict[str, str] = field(default_factory=dict)


@dataclass(frozen=True)
class ChatGPTSignIn:
    subject: str
    email: str | None
    client_id: str
    ext_agent_host_id: str | None
    tokens: TokenSet


@dataclass(frozen=True)
class AuthResult:
    token: Token
    chatgpt: ChatGPTSignIn | None = None


class AuthBackendABC(ABC):
    provider: ClassVar[str]

    def __init__(self, args: dict[str, str]) -> None:
        self._args = args

    @abstractmethod
    async def begin(self, context: AuthContext) -> AuthorizationRequest: ...

    @abstractmethod
    async def complete(
        self,
        *,
        code: str,
        params: Mapping[str, str],
        transaction: Mapping[str, str],
        redirect_uri: str,
    ) -> AuthResult | None: ...
