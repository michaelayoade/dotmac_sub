import uuid
from datetime import UTC, datetime

from sqlalchemy import CheckConstraint, DateTime, Float, ForeignKey, Index, String
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db import Base

FIELD_MOVEMENT_STATUSES = ("en_route", "arrived", "canceled")


class FieldWorkOrderMovement(Base):
    """Technician travel leg for a CRM-synced work-order mirror."""

    __tablename__ = "field_work_order_movements"
    __table_args__ = (
        Index(
            "ix_field_work_order_movements_mirror_started",
            "work_order_mirror_id",
            "started_at",
        ),
        Index("ix_field_work_order_movements_crm_work_order_id", "crm_work_order_id"),
        Index(
            "ix_field_work_order_movements_actor_started",
            "actor_technician_id",
            "started_at",
        ),
        Index("ix_field_work_order_movements_client_ref", "client_ref", unique=True),
        CheckConstraint(
            "status IN ('en_route', 'arrived', 'canceled')",
            name="ck_field_work_order_movements_status",
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
    actor_technician_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("technician_profiles.id"), nullable=False
    )
    actor_person_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), nullable=False
    )
    actor_system_user_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("system_users.id")
    )
    destination_type: Mapped[str] = mapped_column(String(40), nullable=False)
    destination_id: Mapped[str | None] = mapped_column(String(120))
    destination_label: Mapped[str | None] = mapped_column(String(255))
    destination_latitude: Mapped[float | None] = mapped_column(Float)
    destination_longitude: Mapped[float | None] = mapped_column(Float)
    started_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    arrived_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    start_latitude: Mapped[float | None] = mapped_column(Float)
    start_longitude: Mapped[float | None] = mapped_column(Float)
    arrival_latitude: Mapped[float | None] = mapped_column(Float)
    arrival_longitude: Mapped[float | None] = mapped_column(Float)
    status: Mapped[str] = mapped_column(String(20), default="en_route", nullable=False)
    client_ref: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
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
    actor_technician = relationship("TechnicianProfile")
    actor_system_user = relationship("SystemUser")
