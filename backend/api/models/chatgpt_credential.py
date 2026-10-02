from datetime import datetime

from sqlalchemy import Boolean, DateTime, String, Text, func, text
from sqlalchemy.orm import Mapped, mapped_column

from api.core.database import Base


class ChatGPTCredential(Base):
    """OAuth tokens from Sign in with ChatGPT, one row per EVM Bench user.

    Token columns are AES-GCM encrypted with CREDENTIALS_AES_KEY and never leave the backend / oai_proxy.
    """

    __tablename__ = 'chatgpt_credentials'

    user_id: Mapped[str] = mapped_column(String(128), primary_key=True)
    subject: Mapped[str] = mapped_column(String(255))
    email: Mapped[str | None] = mapped_column(String(320))
    client_id: Mapped[str] = mapped_column(String(255))
    ext_agent_host_id: Mapped[str | None] = mapped_column(String(255))
    scopes: Mapped[str] = mapped_column(Text, default='')

    access_token_enc: Mapped[str] = mapped_column(Text)
    refresh_token_enc: Mapped[str | None] = mapped_column(Text)
    id_token_enc: Mapped[str | None] = mapped_column(Text)
    access_expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    earliest_refresh_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    needs_reauth: Mapped[bool] = mapped_column(Boolean, default=False, server_default=text('false'), nullable=False)

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
    )


class AppSetting(Base):
    """Small key/value store for deployment-wide values (e.g. the SIWC ext_agent_host_id)."""

    __tablename__ = 'app_settings'

    key: Mapped[str] = mapped_column(String(64), primary_key=True)
    value: Mapped[str] = mapped_column(Text)
