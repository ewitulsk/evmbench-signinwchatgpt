import enum
import uuid
from datetime import datetime

from sqlalchemy import Boolean, DateTime, Enum, Index, String, Text, Uuid, func, text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from api.core.database import Base


class JobStatus(enum.Enum):
    queued = 'queued'
    running = 'running'
    succeeded = 'succeeded'
    failed = 'failed'


# TODO(es3n1n): revamp
class Job(Base):
    __tablename__ = 'jobs'
    __table_args__ = (Index('ix_jobs_status_created_at_id', 'status', 'created_at', 'id'),)

    id: Mapped[uuid.UUID] = mapped_column(
        Uuid,
        primary_key=True,
        default=uuid.uuid4,
    )

    status: Mapped[JobStatus] = mapped_column(
        Enum(JobStatus),
        default=JobStatus.queued,
        index=True,
    )

    user_id: Mapped[str] = mapped_column(String(128))
    model: Mapped[str] = mapped_column(String(64))
    model_display_name: Mapped[str | None] = mapped_column(String(128))
    # 'api_key' or 'chatgpt_plan'; NULL for jobs created before billing sources existed.
    billing_source: Mapped[str | None] = mapped_column(String(16))
    reasoning_effort: Mapped[str | None] = mapped_column(String(16))
    file_name: Mapped[str] = mapped_column(String(128))
    secret_ref: Mapped[str | None] = mapped_column(String(64))

    result: Mapped[dict | None] = mapped_column(JSONB)
    result_error: Mapped[str | None] = mapped_column(Text)
    # Machine-readable failure reason (e.g. usage_limit_exceeded); first writer wins.
    error_code: Mapped[str | None] = mapped_column(String(64))
    result_received_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    result_token: Mapped[str | None] = mapped_column(String(64))

    public: Mapped[bool] = mapped_column(Boolean, default=False, server_default=text('false'), nullable=False)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
    )
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
