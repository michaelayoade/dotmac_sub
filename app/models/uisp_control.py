"""Desired and observed state for UISP-managed subscriber devices."""

from __future__ import annotations

import enum
import uuid
from datetime import UTC, datetime

from sqlalchemy import (
    JSON,
    Boolean,
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
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db import Base


class UispIntentTargetType(enum.Enum):
    cpe = "cpe"
    ont = "ont"


class UispIntentStatus(enum.Enum):
    staged = "staged"
    applying = "applying"
    pending_readback = "pending_readback"
    pending_observation = "pending_observation"
    verified = "verified"
    drifted = "drifted"
    manual_required = "manual_required"
    failed = "failed"
    decommissioned = "decommissioned"


class UispSnapshotSource(enum.Enum):
    desired = "desired"
    observed = "observed"


def _string_enum(enum_type: type[enum.Enum], name: str):
    return Enum(
        enum_type,
        name=name,
        native_enum=False,
        values_callable=lambda values: [value.value for value in values],
    )


class UispDeviceIntent(Base):
    __tablename__ = "uisp_device_intents"
    __table_args__ = (
        UniqueConstraint("target_type", "target_id", name="uq_uisp_intent_target"),
        Index("ix_uisp_intent_subscription", "subscription_id"),
        Index("ix_uisp_intent_status", "status"),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    target_type: Mapped[UispIntentTargetType] = mapped_column(
        _string_enum(UispIntentTargetType, "uispintenttargettype"), nullable=False
    )
    target_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    subscription_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("subscriptions.id", ondelete="SET NULL"),
        nullable=True,
    )
    service_order_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("service_orders.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )
    uisp_device_id: Mapped[str | None] = mapped_column(String(120), index=True)
    desired_config: Mapped[dict] = mapped_column(JSON, default=dict, nullable=False)
    observed_config: Mapped[dict | None] = mapped_column(JSON)
    drift: Mapped[dict | None] = mapped_column(JSON)
    desired_revision: Mapped[int] = mapped_column(Integer, default=1, nullable=False)
    verified_revision: Mapped[int | None] = mapped_column(Integer)
    status: Mapped[UispIntentStatus] = mapped_column(
        _string_enum(UispIntentStatus, "uispintentstatus"),
        default=UispIntentStatus.staged,
        nullable=False,
    )
    last_error: Mapped[str | None] = mapped_column(Text)
    last_observed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_verified_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(UTC)
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(UTC),
        onupdate=lambda: datetime.now(UTC),
    )

    snapshots: Mapped[list[UispConfigSnapshot]] = relationship(
        back_populates="intent", cascade="all, delete-orphan"
    )


class UispConfigSnapshot(Base):
    __tablename__ = "uisp_config_snapshots"
    __table_args__ = (
        Index("ix_uisp_snapshot_intent_created", "intent_id", "created_at"),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    intent_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("uisp_device_intents.id", ondelete="CASCADE"),
        nullable=False,
    )
    source: Mapped[UispSnapshotSource] = mapped_column(
        _string_enum(UispSnapshotSource, "uispsnapshotsource"), nullable=False
    )
    revision: Mapped[int | None] = mapped_column(Integer)
    config: Mapped[dict] = mapped_column(JSON, nullable=False)
    redacted: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(UTC), nullable=False
    )

    intent: Mapped[UispDeviceIntent] = relationship(back_populates="snapshots")
