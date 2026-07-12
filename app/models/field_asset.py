import uuid
from datetime import UTC, datetime

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    String,
    Text,
    UniqueConstraint,
    text,
)
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship
from sqlalchemy.types import JSON

from app.db import Base

FIELD_ASSET_TYPES = (
    "vehicle",
    "tool",
    "test_equipment",
    "mobile_device",
    "laptop",
    "safety_gear",
    "other",
)
FIELD_ASSET_STATUSES = (
    "available",
    "issued",
    "maintenance",
    "retired",
    "lost",
)
FIELD_ASSET_CUSTODY_STATUSES = (
    "issued",
    "returned",
    "lost",
    "damaged",
)
FIELD_ASSET_CUSTODY_SOURCES = (
    "field_inventory",
    "field_asset",
    "ont",
    "cpe",
    "olt",
    "network_device",
    "router",
)


def _now() -> datetime:
    return datetime.now(UTC)


class FieldAsset(Base):
    """Fleet/tools/equipment issued to field teams, separate from network devices."""

    __tablename__ = "field_assets"
    __table_args__ = (
        UniqueConstraint("asset_tag", name="uq_field_assets_asset_tag"),
        UniqueConstraint("serial_number", name="uq_field_assets_serial_number"),
        Index("ix_field_assets_type_status", "asset_type", "status"),
        Index("ix_field_assets_active", "is_active"),
        CheckConstraint(
            "asset_type IN ('vehicle', 'tool', 'test_equipment', 'mobile_device', "
            "'laptop', 'safety_gear', 'other')",
            name="ck_field_assets_asset_type",
        ),
        CheckConstraint(
            "status IN ('available', 'issued', 'maintenance', 'retired', 'lost')",
            name="ck_field_assets_status",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    asset_tag: Mapped[str] = mapped_column(String(80), nullable=False)
    asset_type: Mapped[str] = mapped_column(String(40), nullable=False)
    name: Mapped[str] = mapped_column(String(160), nullable=False)
    status: Mapped[str] = mapped_column(String(40), default="available", nullable=False)
    vendor: Mapped[str | None] = mapped_column(String(120))
    model: Mapped[str | None] = mapped_column(String(120))
    serial_number: Mapped[str | None] = mapped_column(String(120))
    registration_number: Mapped[str | None] = mapped_column(String(80))
    condition: Mapped[str | None] = mapped_column(String(80))
    notes: Mapped[str | None] = mapped_column(Text)
    metadata_: Mapped[dict | None] = mapped_column("metadata", JSON)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_now, nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_now, onupdate=_now, nullable=False
    )

    custody_events = relationship(
        "FieldAssetCustody",
        back_populates="field_asset",
    )


class FieldAssetCustody(Base):
    """Current and historical custody for field assets and operational devices."""

    __tablename__ = "field_asset_custody"
    __table_args__ = (
        Index("ix_field_asset_custody_asset", "asset_source", "asset_id"),
        Index("ix_field_asset_custody_technician", "technician_id", "status"),
        Index("ix_field_asset_custody_system_user", "system_user_id", "status"),
        Index(
            "uq_field_asset_custody_issued_asset",
            "asset_source",
            "asset_id",
            unique=True,
            postgresql_where=text("status = 'issued'"),
        ),
        CheckConstraint(
            "asset_source IN ('field_inventory', 'field_asset', 'ont', 'cpe', "
            "'olt', 'network_device', 'router')",
            name="ck_field_asset_custody_source",
        ),
        CheckConstraint(
            "status IN ('issued', 'returned', 'lost', 'damaged')",
            name="ck_field_asset_custody_status",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    asset_source: Mapped[str] = mapped_column(String(40), nullable=False)
    asset_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    field_asset_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("field_assets.id", ondelete="CASCADE")
    )
    technician_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("technician_profiles.id"), nullable=False
    )
    system_user_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("system_users.id")
    )
    status: Mapped[str] = mapped_column(String(40), default="issued", nullable=False)
    issued_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_now, nullable=False
    )
    returned_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    condition_on_issue: Mapped[str | None] = mapped_column(String(80))
    condition_on_return: Mapped[str | None] = mapped_column(String(80))
    notes: Mapped[str | None] = mapped_column(Text)
    metadata_: Mapped[dict | None] = mapped_column("metadata", JSON)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_now, nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_now, onupdate=_now, nullable=False
    )

    field_asset = relationship("FieldAsset", back_populates="custody_events")
    technician = relationship("TechnicianProfile")
    system_user = relationship("SystemUser")
