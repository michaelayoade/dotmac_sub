"""Authoritative subscription pause episodes and independently releasable causes."""

from __future__ import annotations

import enum
import uuid
from datetime import UTC, datetime

from sqlalchemy import (
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db import Base


class SubscriptionPauseEpisodeStatus(str, enum.Enum):
    active = "active"
    resumed = "resumed"
    canceled = "canceled"


class SubscriptionPauseCauseStatus(str, enum.Enum):
    active = "active"
    released = "released"
    canceled = "canceled"


class SubscriptionPauseReason(str, enum.Enum):
    ticket_resolution_sla_breach = "ticket_resolution_sla_breach"
    customer_vacation_hold = "customer_vacation_hold"
    administrative = "administrative"


class SubscriptionPauseSource(str, enum.Enum):
    automation_workflow = "automation_workflow"
    customer_portal = "customer_portal"
    administrator = "administrator"


class SubscriptionPauseBillingPolicy(str, enum.Enum):
    extend_by_effective_pause_duration = "extend_by_effective_pause_duration"


class SubscriptionPauseResumePolicy(str, enum.Enum):
    manual_after_ticket_resolution = "manual_after_ticket_resolution"
    scheduled_or_customer_requested = "scheduled_or_customer_requested"
    manual = "manual"


class SubscriptionPauseEpisode(Base):
    """One continuous interval during which access and collection are paused."""

    __tablename__ = "subscription_pause_episodes"
    __table_args__ = (
        Index(
            "uq_subscription_pause_episode_active",
            "subscription_id",
            unique=True,
            postgresql_where=text("status = 'active'"),
        ),
        CheckConstraint(
            "resumed_at IS NULL OR resumed_at >= effective_at",
            name="ck_subscription_pause_episode_nonnegative_duration",
        ),
        CheckConstraint(
            "status <> 'resumed' OR resumed_at IS NOT NULL",
            name="ck_subscription_pause_episode_resumed_at",
        ),
        CheckConstraint(
            "status IN ('active', 'resumed', 'canceled')",
            name="ck_subscription_pause_episode_status",
        ),
        CheckConstraint(
            "effective_duration_seconds IS NULL OR effective_duration_seconds >= 0",
            name="ck_subscription_pause_episode_duration",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    subscription_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("subscriptions.id", ondelete="RESTRICT"),
        nullable=False,
        index=True,
    )
    account_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("subscribers.id", ondelete="RESTRICT"),
        nullable=False,
        index=True,
    )
    status: Mapped[str] = mapped_column(
        String(16), nullable=False, default=SubscriptionPauseEpisodeStatus.active.value
    )
    requested_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    effective_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    resumed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    effective_duration_seconds: Mapped[int | None] = mapped_column(Integer)
    previous_subscription_status: Mapped[str] = mapped_column(
        String(32), nullable=False
    )
    resulting_subscription_status: Mapped[str | None] = mapped_column(String(32))
    previous_account_status: Mapped[str] = mapped_column(String(32), nullable=False)
    resulting_account_status: Mapped[str | None] = mapped_column(String(32))
    previous_next_billing_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True)
    )
    projected_next_billing_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True)
    )
    resulting_next_billing_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True)
    )
    billing_policy_key: Mapped[str] = mapped_column(String(80), nullable=False)
    billing_policy_version: Mapped[int] = mapped_column(
        Integer, nullable=False, default=1
    )
    policy_snapshot: Mapped[dict[str, object]] = mapped_column(
        JSONB, nullable=False, default=dict
    )
    resume_preview_fingerprint: Mapped[str | None] = mapped_column(String(64))
    created_by: Mapped[str] = mapped_column(String(160), nullable=False)
    resumed_by: Mapped[str | None] = mapped_column(String(160))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=lambda: datetime.now(UTC)
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=lambda: datetime.now(UTC),
        onupdate=lambda: datetime.now(UTC),
    )

    causes = relationship(
        "SubscriptionPauseCause",
        back_populates="episode",
        cascade="all, delete-orphan",
    )


class SubscriptionPauseCause(Base):
    """One independently releasable reason keeping a pause episode active."""

    __tablename__ = "subscription_pause_causes"
    __table_args__ = (
        Index(
            "uq_subscription_pause_cause_source",
            "source_type",
            "source_id",
            unique=True,
        ),
        Index(
            "ix_subscription_pause_cause_episode_status",
            "pause_episode_id",
            "status",
        ),
        CheckConstraint(
            "status IN ('active', 'released', 'canceled')",
            name="ck_subscription_pause_cause_status",
        ),
        CheckConstraint(
            "status <> 'released' OR released_at IS NOT NULL",
            name="ck_subscription_pause_cause_released_at",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    pause_episode_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("subscription_pause_episodes.id", ondelete="CASCADE"),
        nullable=False,
    )
    reason_code: Mapped[str] = mapped_column(String(80), nullable=False)
    source_type: Mapped[str] = mapped_column(String(48), nullable=False)
    source_id: Mapped[str] = mapped_column(String(255), nullable=False)
    status: Mapped[str] = mapped_column(
        String(16), nullable=False, default=SubscriptionPauseCauseStatus.active.value
    )
    ticket_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("support_tickets.id", ondelete="RESTRICT"),
        index=True,
    )
    sla_clock_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("sla_clocks.id", ondelete="RESTRICT"),
        index=True,
    )
    sla_breach_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("sla_breaches.id", ondelete="RESTRICT")
    )
    automation_event_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    automation_rule_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    automation_rule_version_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True)
    )
    automation_step_index: Mapped[int | None] = mapped_column(Integer)
    selection_policy: Mapped[str] = mapped_column(String(80), nullable=False)
    resume_policy: Mapped[str] = mapped_column(String(80), nullable=False)
    workflow_policy_snapshot: Mapped[dict[str, object]] = mapped_column(
        JSONB, nullable=False, default=dict
    )
    activated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    scheduled_resume_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), index=True
    )
    released_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    released_by: Mapped[str | None] = mapped_column(String(160))
    release_reason: Mapped[str | None] = mapped_column(Text)
    idempotency_key: Mapped[str] = mapped_column(
        String(255), nullable=False, unique=True
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=lambda: datetime.now(UTC)
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=lambda: datetime.now(UTC),
        onupdate=lambda: datetime.now(UTC),
    )

    episode = relationship("SubscriptionPauseEpisode", back_populates="causes")
