"""Durable schedules for deferred subscription status commands."""

from __future__ import annotations

import enum
import uuid
from datetime import UTC, datetime

from sqlalchemy import (
    JSON,
    DateTime,
    Enum,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.ext.mutable import MutableList
from sqlalchemy.orm import Mapped, mapped_column

from app.db import Base


class SubscriptionLifecycleScheduleStatus(str, enum.Enum):
    pending = "pending"
    processing = "processing"
    applied = "applied"
    canceled = "canceled"
    superseded = "superseded"
    rejected = "rejected"
    failed = "failed"


class SubscriptionLifecycleSchedule(Base):
    """One reviewed, deferred status command and its execution evidence."""

    __tablename__ = "subscription_lifecycle_schedules"
    __table_args__ = (
        UniqueConstraint(
            "subscription_id",
            "idempotency_key",
            name="uq_subscription_lifecycle_schedule_idempotency",
        ),
        Index(
            "ix_subscription_lifecycle_schedule_due",
            "status",
            "next_attempt_at",
            "effective_at",
        ),
        Index(
            "ix_subscription_lifecycle_schedule_subscription",
            "subscription_id",
            "created_at",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    subscription_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("subscriptions.id", ondelete="CASCADE"),
        nullable=False,
    )
    command_kind: Mapped[str] = mapped_column(String(32), nullable=False)
    source: Mapped[str] = mapped_column(String(120), nullable=False)
    effective_timing: Mapped[str] = mapped_column(String(32), nullable=False)
    effective_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    reason: Mapped[str | None] = mapped_column(Text)
    reviewed_head: Mapped[str] = mapped_column(String(64), nullable=False)
    command_fingerprint: Mapped[str] = mapped_column(String(64), nullable=False)
    idempotency_key: Mapped[str | None] = mapped_column(String(160))
    actor_id: Mapped[str | None] = mapped_column(String(120))
    actor_type: Mapped[str] = mapped_column(
        String(32), nullable=False, default="system"
    )

    status: Mapped[SubscriptionLifecycleScheduleStatus] = mapped_column(
        Enum(
            SubscriptionLifecycleScheduleStatus,
            native_enum=False,
            length=32,
        ),
        nullable=False,
        default=SubscriptionLifecycleScheduleStatus.pending,
    )
    attempt_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    max_attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=5)
    next_attempt_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    claimed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    claim_expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    claimed_by: Mapped[str | None] = mapped_column(String(120))

    last_error_code: Mapped[str | None] = mapped_column(String(120))
    last_message: Mapped[str | None] = mapped_column(Text)
    outcome_head: Mapped[str | None] = mapped_column(String(64))
    artifact_ids: Mapped[list[str] | None] = mapped_column(
        MutableList.as_mutable(JSON())
    )
    applied_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    canceled_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    canceled_by: Mapped[str | None] = mapped_column(String(120))

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(UTC), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(UTC),
        onupdate=lambda: datetime.now(UTC),
        nullable=False,
    )
