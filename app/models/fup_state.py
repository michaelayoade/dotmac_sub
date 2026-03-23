"""FUP runtime state — tracks per-subscription enforcement posture."""

from __future__ import annotations

import enum
import uuid
from datetime import UTC, datetime

from sqlalchemy import (
    DateTime,
    Enum,
    Float,
    ForeignKey,
    Text,
    UniqueConstraint,
)
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db import Base


class FupActionStatus(enum.Enum):
    none = "none"
    throttled = "throttled"
    blocked = "blocked"
    notified = "notified"


class FupState(Base):
    """Per-subscription FUP enforcement state.

    One row per subscription, upserted when usage evaluation triggers
    a threshold.  Persists the current enforcement posture so that
    the system can survive restarts and correctly restore/reset
    throttles at period boundaries.
    """

    __tablename__ = "fup_states"
    __table_args__ = (
        UniqueConstraint("subscription_id", name="uq_fup_states_subscription"),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    subscription_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("subscriptions.id", ondelete="CASCADE"),
        nullable=False,
    )
    offer_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("catalog_offers.id"),
        nullable=False,
    )

    # Currently triggered rule (null = no threshold breached)
    active_rule_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("fup_rules.id", ondelete="SET NULL"),
        nullable=True,
    )

    action_status: Mapped[FupActionStatus] = mapped_column(
        Enum(FupActionStatus, name="fupactionstatus", create_constraint=False),
        nullable=False,
        default=FupActionStatus.none,
    )
    speed_reduction_percent: Mapped[float | None] = mapped_column(Float, nullable=True)

    # RADIUS profile applied before throttle (to restore on reset)
    original_profile_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("radius_profiles.id", ondelete="SET NULL"),
        nullable=True,
    )

    # Throttle RADIUS profile currently applied
    throttle_profile_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("radius_profiles.id", ondelete="SET NULL"),
        nullable=True,
    )

    # Period boundary — when the current FUP period resets
    cap_resets_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    last_evaluated_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    # Human-readable note (e.g. "80% download quota reached")
    notes: Mapped[str | None] = mapped_column(Text, nullable=True)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(UTC),
        nullable=False,
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(UTC),
        onupdate=lambda: datetime.now(UTC),
        nullable=False,
    )

    subscription = relationship("Subscription", backref="fup_state", uselist=False)
    offer = relationship("CatalogOffer")
    active_rule = relationship("FupRule")
    original_profile = relationship("RadiusProfile", foreign_keys=[original_profile_id])
    throttle_profile = relationship("RadiusProfile", foreign_keys=[throttle_profile_id])
