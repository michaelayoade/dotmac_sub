import uuid
from datetime import UTC, datetime

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
)
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship
from sqlalchemy.types import JSON

from app.db import Base

FIELD_MATERIAL_STATUSES = ("required", "reserved", "used")


class FieldInventoryItem(Base):
    """Minimal field-material catalog item used before the full inventory port."""

    __tablename__ = "field_inventory_items"
    __table_args__ = (
        Index("ix_field_inventory_items_sku", "sku"),
        Index("ix_field_inventory_items_name", "name"),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    crm_item_id: Mapped[str | None] = mapped_column(String(64), unique=True)
    sku: Mapped[str | None] = mapped_column(String(80))
    name: Mapped[str] = mapped_column(String(160), nullable=False)
    unit: Mapped[str | None] = mapped_column(String(40))
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    metadata_: Mapped[dict | None] = mapped_column("metadata", JSON)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(UTC), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(UTC),
        onupdate=lambda: datetime.now(UTC),
        nullable=False,
    )


class FieldWorkOrderMaterial(Base):
    """Allocated material line for a CRM-synced field work-order mirror."""

    __tablename__ = "field_work_order_materials"
    __table_args__ = (
        Index(
            "ix_field_work_order_materials_mirror",
            "work_order_mirror_id",
            "created_at",
        ),
        Index("ix_field_work_order_materials_crm_work_order_id", "crm_work_order_id"),
        Index("ix_field_work_order_materials_item", "item_id"),
        Index("ix_field_work_order_materials_crm_material_id", "crm_material_id"),
        Index("ix_field_work_order_materials_status", "status"),
        CheckConstraint(
            "allocated_quantity >= 0",
            name="ck_field_work_order_materials_allocated_nonnegative",
        ),
        CheckConstraint(
            "consumed_quantity >= 0",
            name="ck_field_work_order_materials_consumed_nonnegative",
        ),
        CheckConstraint(
            "consumed_quantity <= allocated_quantity",
            name="ck_field_work_order_materials_consumed_lte_allocated",
        ),
        CheckConstraint(
            "status IN ('required', 'reserved', 'used')",
            name="ck_field_work_order_materials_status",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    work_order_mirror_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("work_order_mirror.id", ondelete="CASCADE"),
        nullable=False,
    )
    crm_work_order_id: Mapped[str] = mapped_column(String(64), nullable=False)
    crm_material_id: Mapped[str | None] = mapped_column(String(64))
    item_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("field_inventory_items.id"), nullable=False
    )
    allocated_quantity: Mapped[int] = mapped_column(Integer, nullable=False)
    consumed_quantity: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    status: Mapped[str] = mapped_column(String(30), default="required", nullable=False)
    notes: Mapped[str | None] = mapped_column(Text)
    metadata_: Mapped[dict | None] = mapped_column("metadata", JSON)
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

    work_order_mirror = relationship("WorkOrderMirror")
    item = relationship("FieldInventoryItem")
