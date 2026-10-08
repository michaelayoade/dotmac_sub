"""Customer geographic region configuration."""

from __future__ import annotations

import enum
import uuid
from datetime import UTC, datetime

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Integer,
    String,
    Text,
)
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db import Base


class CustomerRegionMatchMode(enum.Enum):
    """Tie-breaker used when a customer is inside multiple region radii."""

    nearest = "nearest"
    nas = "nas"
    pop_site = "pop_site"
    manual = "manual"


class CustomerRegion(Base):
    """A configurable circular customer region."""

    __tablename__ = "customer_regions"
    __table_args__ = (
        CheckConstraint(
            "latitude >= -90 AND latitude <= 90", name="ck_customer_region_latitude"
        ),
        CheckConstraint(
            "longitude >= -180 AND longitude <= 180",
            name="ck_customer_region_longitude",
        ),
        CheckConstraint("radius_meters > 0", name="ck_customer_region_radius"),
        CheckConstraint("length(color) = 7", name="ck_customer_region_color"),
        CheckConstraint(
            "match_mode IN ('nearest', 'nas', 'pop_site', 'manual')",
            name="ck_customer_region_match_mode",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    name: Mapped[str] = mapped_column(String(160), nullable=False, unique=True)
    latitude: Mapped[float] = mapped_column(nullable=False)
    longitude: Mapped[float] = mapped_column(nullable=False)
    radius_meters: Mapped[float] = mapped_column(nullable=False, default=300)
    color: Mapped[str] = mapped_column(String(7), nullable=False, default="#0ea5e9")
    match_mode: Mapped[str] = mapped_column(
        String(20), nullable=False, default="nearest"
    )
    priority: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    nas_device_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("nas_devices.id", ondelete="SET NULL")
    )
    pop_site_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("pop_sites.id", ondelete="SET NULL")
    )
    notes: Mapped[str | None] = mapped_column(Text)
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=lambda: datetime.now(UTC)
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=lambda: datetime.now(UTC),
        onupdate=lambda: datetime.now(UTC),
    )

    nas_device = relationship("NasDevice")
    pop_site = relationship("PopSite")
