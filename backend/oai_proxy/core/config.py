from pydantic import AliasChoices, Field, PostgresDsn, Secret, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from api.util.fs import ROOT_DIR


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=ROOT_DIR / '.env',
        env_file_encoding='utf-8',
        extra='ignore',
    )

    OAI_PROXY_HOST: str = '127.0.0.1'
    OAI_PROXY_PORT: int = 8084
    OAI_PROXY_WORKERS: int = 1
    OAI_PROXY_AES_KEY: Secret[str]
    OAI_SHARED_KEY_ENABLED: bool = False
    # Static OpenAI key - when set, requests with "Bearer STATIC" use this key
    # The real key never leaves this service
    OAI_PROXY_STATIC_KEY: Secret[str] | None = None

    # ChatGPT-plan (SIWC) jobs: the proxy brokers the user's ChatGPT access token. Needs the job database,
    # the credential encryption key, and the same AUTH_BACKEND_ARGUMENTS as the API (for token refresh).
    DATABASE_DSN: Secret[PostgresDsn] | None = None
    OAI_PROXY_DATABASE_POOL_SIZE: int = Field(
        default=5,
        validation_alias=AliasChoices('OAI_PROXY_DATABASE_POOL_SIZE', 'DATABASE_POOL_SIZE'),
    )
    CREDENTIALS_AES_KEY: Secret[str] | None = None
    AUTH_BACKEND: str | None = None
    AUTH_BACKEND_ARGUMENTS: dict[str, str] = Field(default_factory=dict)

    @property
    def siwc_enabled(self) -> bool:
        return self.AUTH_BACKEND == 'chatgpt' and self.DATABASE_DSN is not None and self.CREDENTIALS_AES_KEY is not None

    @model_validator(mode='after')
    def _disable_shared_key(self) -> 'Settings':
        if not self.OAI_SHARED_KEY_ENABLED:
            self.OAI_PROXY_STATIC_KEY = None
        return self


settings = Settings()  # type: ignore[missing-argument]
