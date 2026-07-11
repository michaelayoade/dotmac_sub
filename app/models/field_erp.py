import uuid
from datetime import UTC, datetime

from sqlalchemy import (
    CheckConstraint,
    DateTime,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column
from sqlalchemy.types import JSON

from app.db import Base

FIELD_ERP_SYNC_EVENT_STATUSES = (
    "pending",
    "processing",
    "synced",
    "failed",
    "canceled",
)


def _now() -> datetime:
    return datetime.now(UTC)


class FieldErpSyncEvent(Base):
    """Outbox event for field-service writes that must be reflected in ERP."""

    __tablename__ = "field_erp_sync_events"
    __table_args__ = (
        UniqueConstraint("idempotency_key", name="uq_field_erp_sync_events_key"),
        Index("ix_field_erp_sync_events_status", "status", "created_at"),
        Index(
            "ix_field_erp_sync_events_entity",
            "entity_type",
            "entity_id",
            "action",
        ),
        CheckConstraint(
            "status IN ('pending', 'processing', 'synced', 'failed', 'canceled')",
            name="ck_field_erp_sync_events_status",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    entity_type: Mapped[str] = mapped_column(String(80), nullable=False)
    entity_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    action: Mapped[str] = mapped_column(String(80), nullable=False)
    status: Mapped[str] = mapped_column(String(40), default="pending", nullable=False)
    idempotency_key: Mapped[str] = mapped_column(String(180), nullable=False)
    payload: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)
    attempts: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    remote_id: Mapped[str | None] = mapped_column(String(120))
    remote_number: Mapped[str | None] = mapped_column(String(120))
    remote_status: Mapped[str | None] = mapped_column(String(80))
    last_error: Mapped[str | None] = mapped_column(Text)
    last_attempt_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    synced_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_now, nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_now, onupdate=_now, nullable=False
    )
