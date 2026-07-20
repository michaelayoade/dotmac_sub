"""Durable implementation-to-customer-experience handoff evidence."""

from __future__ import annotations

import enum
import uuid
from datetime import UTC, datetime

from sqlalchemy import (
    JSON,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    event,
)
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db import Base


class CustomerExperienceHandoffStatus(enum.StrEnum):
    pending = "pending"
    ready = "ready"
    accepted = "accepted"
    needs_attention = "needs_attention"
    canceled = "canceled"


class CustomerExperienceHandoff(Base):
    __tablename__ = "customer_experience_handoffs"
    __table_args__ = (
        UniqueConstraint("subscription_id", name="uq_cx_handoffs_subscription"),
        UniqueConstraint("service_order_id", name="uq_cx_handoffs_service_order"),
        CheckConstraint(
            "status IN ('pending', 'ready', 'accepted', 'needs_attention', 'canceled')",
            name="ck_cx_handoffs_status",
        ),
        CheckConstraint("policy_version >= 1", name="ck_cx_handoffs_policy_version"),
        Index("ix_cx_handoffs_subscriber_status", "subscriber_id", "status"),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    subscriber_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("subscribers.id", ondelete="RESTRICT"),
        nullable=False,
    )
    subscription_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("subscriptions.id", ondelete="RESTRICT"),
        nullable=False,
    )
    sales_order_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("sales_orders.id", ondelete="RESTRICT"),
        nullable=False,
    )
    project_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("projects.id", ondelete="RESTRICT"),
        nullable=False,
    )
    installation_project_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("installation_projects.id", ondelete="RESTRICT"),
        nullable=False,
    )
    service_order_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("service_orders.id", ondelete="RESTRICT"),
        nullable=False,
    )
    status: Mapped[str] = mapped_column(
        String(24),
        default=CustomerExperienceHandoffStatus.pending.value,
        nullable=False,
    )
    policy_version: Mapped[int] = mapped_column(Integer, default=1, nullable=False)
    readiness_evidence: Mapped[dict] = mapped_column(JSON, default=dict, nullable=False)
    ready_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    accepted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    accepted_by_actor_type: Mapped[str | None] = mapped_column(String(40))
    accepted_by_actor_id: Mapped[str | None] = mapped_column(String(160))
    attention_reason: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(UTC), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(UTC),
        onupdate=lambda: datetime.now(UTC),
        nullable=False,
    )

    subscriber = relationship("Subscriber")
    subscription = relationship("Subscription")
    sales_order = relationship("SalesOrder")
    project = relationship("Project")
    installation_project = relationship("InstallationProject")
    service_order = relationship("ServiceOrder")
    lifecycle_events = relationship(
        "CustomerExperienceHandoffEvent",
        back_populates="handoff",
        order_by="CustomerExperienceHandoffEvent.occurred_at",
    )


class CustomerExperienceHandoffEvent(Base):
    __tablename__ = "customer_experience_handoff_events"
    __table_args__ = (
        CheckConstraint("from_status <> to_status", name="ck_cx_handoff_event_change"),
        Index("ix_cx_handoff_events_event_id", "event_id", unique=True),
        Index("ix_cx_handoff_events_event_type", "event_type"),
        Index("ix_cx_handoff_events_actor_id", "actor_id"),
        Index("ix_cx_handoff_events_handoff_occurred", "handoff_id", "occurred_at"),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    event_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    handoff_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("customer_experience_handoffs.id", ondelete="RESTRICT"),
        nullable=False,
    )
    event_type: Mapped[str] = mapped_column(String(100), nullable=False)
    from_status: Mapped[str] = mapped_column(String(24), nullable=False)
    to_status: Mapped[str] = mapped_column(String(24), nullable=False)
    actor_type: Mapped[str] = mapped_column(String(40), nullable=False)
    actor_id: Mapped[str] = mapped_column(String(160), nullable=False)
    reason: Mapped[str | None] = mapped_column(Text)
    decision_context: Mapped[dict | None] = mapped_column(JSON)
    occurred_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(UTC), nullable=False
    )

    handoff = relationship(
        "CustomerExperienceHandoff", back_populates="lifecycle_events"
    )


class CustomerExperienceHandoffEventImmutableError(RuntimeError):
    pass


@event.listens_for(CustomerExperienceHandoffEvent, "before_update")
def _reject_cx_event_update(*_args: object) -> None:
    raise CustomerExperienceHandoffEventImmutableError(
        "Customer-experience handoff evidence is append-only"
    )


@event.listens_for(CustomerExperienceHandoffEvent, "before_delete")
def _reject_cx_event_delete(*_args: object) -> None:
    raise CustomerExperienceHandoffEventImmutableError(
        "Customer-experience handoff evidence is append-only"
    )
