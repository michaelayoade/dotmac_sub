"""Dead-letter for terminal CRM push failures.

The CRM push task (app.tasks.crm_sync.push_subscriber_change) retries with
backoff; when retries are exhausted the change would otherwise vanish into a
log line (silent drift). A row here records it so it is visible and
re-drivable — for BOTH the event-driven and nightly-billing push paths.
"""

import enum
import uuid
from datetime import UTC, datetime

from sqlalchemy import JSON, DateTime, Enum, Integer, String, Text
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db import Base


class CrmSyncFailureStatus(enum.Enum):
    unresolved = "unresolved"
    resolved = "resolved"


class CrmSyncFailure(Base):
    __tablename__ = "crm_sync_failures"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    entity: Mapped[str] = mapped_column(String(40), default="subscriber")
    external_id: Mapped[str] = mapped_column(String(120), nullable=False)
    external_system: Mapped[str] = mapped_column(String(40), nullable=False)
    payload: Mapped[dict | None] = mapped_column(JSON)
    error: Mapped[str | None] = mapped_column(Text)
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    status: Mapped[CrmSyncFailureStatus] = mapped_column(
        Enum(CrmSyncFailureStatus),
        default=CrmSyncFailureStatus.unresolved,
        nullable=False,
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(UTC)
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(UTC),
        onupdate=lambda: datetime.now(UTC),
    )
    resolved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
