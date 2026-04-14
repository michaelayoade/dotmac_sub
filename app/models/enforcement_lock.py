"""Enforcement lock model for subscription lifecycle state machine.

Tracks structured reasons why a subscription is suspended. A subscription
is considered suspended if it has ANY active enforcement lock. Restoration
requires resolving all active locks, and each lock's reason determines
which triggers are allowed to resolve it.
"""

from __future__ import annotations

import enum
import uuid
from datetime import UTC, datetime

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    DateTime,
    Enum,
    ForeignKey,
    Index,
    String,
    Text,
    text,
)
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db import Base


class EnforcementReason(enum.Enum):
    """Why a subscription was suspended."""

    overdue = "overdue"  # Non-payment / dunning
    fup = "fup"  # Fair usage policy exhausted
    prepaid = "prepaid"  # Prepaid balance depleted
    admin = "admin"  # Manual admin suspension
    customer_hold = "customer_hold"  # Customer-initiated vacation hold
    fraud = "fraud"  # Fraud / abuse investigation
    system = "system"  # System-level (migration, maintenance)


class EnforcementLock(Base):
    """A single enforcement lock on a subscription.

    Multiple locks can be active simultaneously (e.g., overdue + FUP).
    The subscription remains suspended until ALL active locks are resolved.
    Each lock records the reason, source, and resolution details for audit.

    Invariants enforced at DB level:
    - Resolved locks must have resolved_at and resolved_by populated.
    - Only one active lock per subscription+reason (partial unique index).
    """

    __tablename__ = "enforcement_locks"
    __table_args__ = (
        Index(
            "ix_enforcement_locks_subscription_active",
            "subscription_id",
            "is_active",
        ),
        Index(
            "ix_enforcement_locks_subscriber_active",
            "subscriber_id",
            "is_active",
        ),
        # Prevent duplicate active locks for same reason on same subscription
        Index(
            "uq_enforcement_locks_active_reason",
            "subscription_id",
            "reason",
            unique=True,
            postgresql_where=text("is_active = true"),
        ),
        # Resolved locks must have resolution metadata
        CheckConstraint(
            "is_active = true OR (resolved_at IS NOT NULL AND resolved_by IS NOT NULL)",
            name="ck_enforcement_locks_resolved_metadata",
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
    subscriber_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("subscribers.id", ondelete="CASCADE"),
        nullable=False,
    )
    reason: Mapped[EnforcementReason] = mapped_column(
        Enum(EnforcementReason, name="enforcementreason", create_constraint=False),
        nullable=False,
    )
    source: Mapped[str] = mapped_column(
        String(255), nullable=False
    )  # "dunning_case:{id}", "admin:{user_id}", "fup_rule:{id}"
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(UTC), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(UTC),
        onupdate=lambda: datetime.now(UTC),
        nullable=False,
    )
    resolved_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    resolved_by: Mapped[str | None] = mapped_column(
        String(255), nullable=True
    )  # "payment:{id}", "admin:{user_id}", "cap_reset"
    resume_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )  # Scheduled auto-resume for vacation holds
    notes: Mapped[str | None] = mapped_column(Text, nullable=True)

    subscription = relationship("Subscription", backref="enforcement_locks")
    subscriber = relationship("Subscriber", backref="enforcement_locks")
