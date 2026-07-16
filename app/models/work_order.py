"""Work-order headers and their field-execution activity.

Sub owns work orders. ``public_id`` is the identity; ``crm_work_order_id`` is a
nullable reference to the CRM row a header was imported from during migration,
and is NULL for natively created work orders. Field activity (worklogs, notes,
attachments, materials, movements, fiber tests, chat, job events) hangs off
``work_order.id`` and has no upstream to rebuild from, so this table is
authoritative storage, not a cache.
"""

import uuid
from datetime import UTC, datetime

from sqlalchemy import Boolean, ForeignKey, Integer, String, Text
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column
from sqlalchemy.types import JSON, DateTime

from app.db import Base


def _default_public_id(context) -> str:
    """Seed public_id from the CRM provenance ref when a row is imported
    (matching migration 328's backfill), else mint a native ``sub-`` id."""
    crm_id = context.get_current_parameters().get("crm_work_order_id")
    return crm_id or f"sub-{uuid.uuid4().hex}"


class WorkOrder(Base):
    """One work order attributed to one of our subscribers."""

    __tablename__ = "work_order"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    public_id: Mapped[str] = mapped_column(
        String(64),
        nullable=False,
        unique=True,
        index=True,
        default=_default_public_id,
    )
    crm_work_order_id: Mapped[str | None] = mapped_column(
        String(64), nullable=True, unique=True, index=True
    )
    subscriber_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("subscribers.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    title: Mapped[str] = mapped_column(String(200), nullable=False, default="")
    description: Mapped[str | None] = mapped_column(Text)
    # Values are declared by app.services.field.work_order_status.WorkOrderStatus.
    status: Mapped[str] = mapped_column(String(20), nullable=False, default="scheduled")
    work_type: Mapped[str | None] = mapped_column(String(20))
    priority: Mapped[str | None] = mapped_column(String(20))
    crm_ticket_id: Mapped[str | None] = mapped_column(String(64), index=True)
    crm_project_id: Mapped[str | None] = mapped_column(String(64), index=True)
    assigned_to_crm_person_id: Mapped[str | None] = mapped_column(
        String(64), index=True
    )
    assigned_to_name: Mapped[str | None] = mapped_column(String(160))
    technician_name: Mapped[str | None] = mapped_column(String(160))
    technician_phone: Mapped[str | None] = mapped_column(String(40))
    address: Mapped[str | None] = mapped_column(String(255))
    scheduled_start: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    scheduled_end: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    estimated_arrival_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True)
    )
    estimated_duration_minutes: Mapped[int | None] = mapped_column(Integer)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    paused_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    resumed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    total_active_seconds: Mapped[int | None] = mapped_column(Integer)
    required_skills: Mapped[list | None] = mapped_column(JSON)
    tags: Mapped[list | None] = mapped_column(JSON)
    access_notes: Mapped[str | None] = mapped_column(Text)
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    metadata_: Mapped[dict | None] = mapped_column("metadata", JSON)
    work_order_created_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True)
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(UTC)
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(UTC),
        onupdate=lambda: datetime.now(UTC),
    )


class WorkOrderSyncState(Base):
    """Per-subscriber reconcile marker — drives the lazy on-view refresh TTL even
    when the subscriber has zero work orders."""

    __tablename__ = "work_order_sync_state"

    subscriber_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("subscribers.id", ondelete="CASCADE"),
        primary_key=True,
    )
    synced_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(UTC),
        onupdate=lambda: datetime.now(UTC),
    )
