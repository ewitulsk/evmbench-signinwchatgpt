from api.auth import AuthBackendABC, auth_backends
from api.auth.chatgpt import ChatGPTAuthBackend
from api.core.config import settings
from api.secrets import SecretStorageABC, secret_storages
from api.siwc.credentials import CredentialStore


secret_storage: SecretStorageABC = secret_storages[settings.BACKEND_SECRETS_BACKEND](
    settings.BACKEND_SECRETS_BACKEND_ARGUMENTS,
)

auth_backend: AuthBackendABC | None = (
    auth_backends[settings.AUTH_BACKEND](
        settings.AUTH_BACKEND_ARGUMENTS,
    )
    if settings.AUTH_BACKEND
    else None
)

credential_store: CredentialStore | None = (
    CredentialStore(settings.CREDENTIALS_AES_KEY.get_secret_value()) if settings.CREDENTIALS_AES_KEY else None
)


def chatgpt_auth() -> tuple[ChatGPTAuthBackend, CredentialStore] | None:
    """The ChatGPT auth backend and credential store, when Sign in with ChatGPT is configured."""
    if isinstance(auth_backend, ChatGPTAuthBackend) and credential_store is not None:
        return auth_backend, credential_store
    return None
