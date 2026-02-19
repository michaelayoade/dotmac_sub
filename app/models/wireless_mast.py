import enum
import uuid
from datetime import UTC, datetime

from geoalchemy2 import Geometry
from sqlalchemy import JSON, Boolean, DateTime, Enum, Float, ForeignKey, String, Text
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db import Base


class WirelessMastStatus(enum.Enum):
    active = "active"
    inactive = "inactive"
    maintenance = "maintenance"
    decommissioned = "decommissioned"


class WirelessMast(Base):
    __tablename__ = "wireless_masts"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    name: Mapped[str] = mapped_column(String(160), nullable=False)
    latitude: Mapped[float] = mapped_column(Float, nullable=False)
    longitude: Mapped[float] = mapped_column(Float, nullable=False)
    geom = mapped_column(Geometry("POINT", srid=4326), nullable=True)
    height_m: Mapped[float | None] = mapped_column(Float)
    structure_type: Mapped[str | None] = mapped_column(String(80))
    owner: Mapped[str | None] = mapped_column(String(160))
    status: Mapped[WirelessMastStatus] = mapped_column(
        Enum(WirelessMastStatus, values_callable=lambda x: [e.value for e in x]),
        nullable=False,
        default=WirelessMastStatus.active,
    )
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    notes: Mapped[str | None] = mapped_column(Text)
    metadata_: Mapped[dict | None] = mapped_column("metadata", JSON)
    pop_site_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("pop_sites.id")
    )

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(UTC)
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(UTC),
        onupdate=lambda: datetime.now(UTC),
    )

    pop_site = relationship("PopSite", backref="masts")
