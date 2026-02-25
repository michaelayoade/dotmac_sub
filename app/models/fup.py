"""Fair Usage Policy models."""

from __future__ import annotations

import enum
import uuid
from datetime import UTC, datetime, time

from sqlalchemy import (
    Boolean,
    DateTime,
    Enum,
    Float,
    ForeignKey,
    Integer,
    String,
    Text,
    Time,
)
from sqlalchemy.dialects.postgresql import ARRAY, UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db import Base


class FupConsumptionPeriod(enum.Enum):
    monthly = "monthly"
    daily = "daily"
    weekly = "weekly"


class FupDirection(enum.Enum):
    up = "up"
    down = "down"
    up_down = "up_down"


class FupAction(enum.Enum):
    reduce_speed = "reduce_speed"
    block = "block"
    notify = "notify"


class FupDataUnit(enum.Enum):
    mb = "mb"
    gb = "gb"
    tb = "tb"


class FupPolicy(Base):
    """Fair Usage Policy configuration linked to a catalog offer."""

    __tablename__ = "fup_policies"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    offer_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("catalog_offers.id"),
        nullable=False,
        unique=True,
    )

    # Traffic accounting window
    traffic_accounting_start: Mapped[time | None] = mapped_column(
        Time, nullable=True
    )
    traffic_accounting_end: Mapped[time | None] = mapped_column(
        Time, nullable=True
    )
    traffic_inverse_interval: Mapped[bool] = mapped_column(Boolean, default=False)

    # Online time accounting window
    online_accounting_start: Mapped[time | None] = mapped_column(
        Time, nullable=True
    )
    online_accounting_end: Mapped[time | None] = mapped_column(
        Time, nullable=True
    )
    online_inverse_interval: Mapped[bool] = mapped_column(Boolean, default=False)

    # Days of week (stored as array of day numbers 0=Mon .. 6=Sun)
    traffic_days_of_week: Mapped[list[int] | None] = mapped_column(
        ARRAY(Integer), nullable=True
    )
    online_days_of_week: Mapped[list[int] | None] = mapped_column(
        ARRAY(Integer), nullable=True
    )

    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
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

    offer = relationship("CatalogOffer", backref="fup_policy")
    rules: Mapped[list[FupRule]] = relationship(
        "FupRule",
        back_populates="policy",
        cascade="all, delete-orphan",
        order_by="FupRule.sort_order",
    )


class FupRule(Base):
    """Individual FUP rule - triggers action on consumption threshold."""

    __tablename__ = "fup_rules"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    policy_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("fup_policies.id", ondelete="CASCADE"),
        nullable=False,
    )

    name: Mapped[str] = mapped_column(String(120), nullable=False)
    sort_order: Mapped[int] = mapped_column(Integer, default=0)

    # Trigger condition
    consumption_period: Mapped[FupConsumptionPeriod] = mapped_column(
        Enum(
            FupConsumptionPeriod,
            name="fupconsumptionperiod",
            create_constraint=False,
        ),
        nullable=False,
        default=FupConsumptionPeriod.monthly,
    )
    direction: Mapped[FupDirection] = mapped_column(
        Enum(FupDirection, name="fupdirection", create_constraint=False),
        nullable=False,
        default=FupDirection.up_down,
    )
    threshold_amount: Mapped[float] = mapped_column(Float, nullable=False)
    threshold_unit: Mapped[FupDataUnit] = mapped_column(
        Enum(FupDataUnit, name="fupdataunit", create_constraint=False),
        nullable=False,
        default=FupDataUnit.gb,
    )

    # Action
    action: Mapped[FupAction] = mapped_column(
        Enum(FupAction, name="fupaction", create_constraint=False),
        nullable=False,
        default=FupAction.reduce_speed,
    )
    speed_reduction_percent: Mapped[float | None] = mapped_column(
        Float, nullable=True
    )

    is_active: Mapped[bool] = mapped_column(Boolean, default=True)

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

    policy: Mapped[FupPolicy] = relationship("FupPolicy", back_populates="rules")
