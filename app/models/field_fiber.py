import uuid
from datetime import UTC, datetime

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
)
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db import Base

FIELD_FIBER_TEST_TYPES = (
    "otdr",
    "optical_power",
    "insertion_loss",
    "reflectance",
    "continuity",
    "other",
)


class FieldFiberTestResult(Base):
    __tablename__ = "field_fiber_test_results"
    __table_args__ = (
        Index(
            "ix_field_fiber_tests_mirror_created", "work_order_mirror_id", "created_at"
        ),
        Index("ix_field_fiber_tests_crm_work_order_id", "crm_work_order_id"),
        Index("ix_field_fiber_tests_asset", "asset_type", "asset_id"),
        Index("ix_field_fiber_tests_client_ref", "client_ref", unique=True),
        CheckConstraint(
            "test_type IN ('otdr', 'optical_power', 'insertion_loss', 'reflectance', 'continuity', 'other')",
            name="ck_field_fiber_tests_test_type",
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
    asset_type: Mapped[str] = mapped_column(String(80), nullable=False)
    asset_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    test_type: Mapped[str] = mapped_column(String(40), nullable=False)
    wavelength_nm: Mapped[int | None] = mapped_column(Integer)
    value_db: Mapped[float | None] = mapped_column(Float)
    unit: Mapped[str | None] = mapped_column(String(16))
    passed: Mapped[bool | None] = mapped_column(Boolean)
    instrument: Mapped[str | None] = mapped_column(String(120))
    attachment_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    measured_by_technician_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("technician_profiles.id"), nullable=False
    )
    measured_by_person_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), nullable=False
    )
    measured_by_system_user_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("system_users.id"),
    )
    measured_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    notes: Mapped[str | None] = mapped_column(Text)
    client_ref: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(UTC)
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(UTC),
        onupdate=lambda: datetime.now(UTC),
    )

    work_order = relationship("WorkOrderMirror")
    technician = relationship("TechnicianProfile")
