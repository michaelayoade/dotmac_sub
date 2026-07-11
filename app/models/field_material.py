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
FIELD_MATERIAL_REQUEST_STATUSES = (
    "draft",
    "submitted",
    "approved",
    "rejected",
    "issued",
    "fulfilled",
    "canceled",
)
FIELD_MATERIAL_REQUEST_PRIORITIES = ("low", "medium", "high", "urgent")


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


class FieldMaterialRequest(Base):
    """Technician material request attached to a CRM-synced work-order mirror."""

    __tablename__ = "field_material_requests"
    __table_args__ = (
        Index(
            "ix_field_material_requests_mirror", "work_order_mirror_id", "created_at"
        ),
        Index("ix_field_material_requests_crm_work_order_id", "crm_work_order_id"),
        Index("ix_field_material_requests_status", "status"),
        Index("ix_field_material_requests_requested_by", "requested_by_technician_id"),
        CheckConstraint(
            "status IN ('draft', 'submitted', 'approved', 'rejected', 'issued', "
            "'fulfilled', 'canceled')",
            name="ck_field_material_requests_status",
        ),
        CheckConstraint(
            "priority IN ('low', 'medium', 'high', 'urgent')",
            name="ck_field_material_requests_priority",
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
    crm_material_request_id: Mapped[str | None] = mapped_column(String(64), unique=True)
    erp_material_request_id: Mapped[str | None] = mapped_column(String(120))
    erp_material_status: Mapped[str | None] = mapped_column(String(40))
    requested_by_technician_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("technician_profiles.id"), nullable=False
    )
    requested_by_person_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), nullable=False
    )
    requested_by_system_user_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("system_users.id")
    )
    status: Mapped[str] = mapped_column(String(30), default="draft", nullable=False)
    priority: Mapped[str] = mapped_column(String(20), default="medium", nullable=False)
    notes: Mapped[str | None] = mapped_column(Text)
    metadata_: Mapped[dict | None] = mapped_column("metadata", JSON)
    submitted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    approved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    rejected_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    fulfilled_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
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
    requested_by_technician = relationship("TechnicianProfile")
    requested_by_system_user = relationship("SystemUser")
    items = relationship(
        "FieldMaterialRequestItem",
        back_populates="material_request",
        cascade="all, delete-orphan",
    )


class FieldMaterialRequestItem(Base):
    __tablename__ = "field_material_request_items"
    __table_args__ = (
        Index(
            "ix_field_material_request_items_request",
            "material_request_id",
            "created_at",
        ),
        Index("ix_field_material_request_items_item", "item_id"),
        CheckConstraint(
            "quantity > 0",
            name="ck_field_material_request_items_quantity_positive",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    material_request_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("field_material_requests.id", ondelete="CASCADE"),
        nullable=False,
    )
    item_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("field_inventory_items.id"), nullable=False
    )
    quantity: Mapped[int] = mapped_column(Integer, nullable=False)
    notes: Mapped[str | None] = mapped_column(Text)
    metadata_: Mapped[dict | None] = mapped_column("metadata", JSON)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(UTC), nullable=False
    )

    material_request = relationship("FieldMaterialRequest", back_populates="items")
    item = relationship("FieldInventoryItem")
