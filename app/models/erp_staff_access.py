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
    UniqueConstraint,
)
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db import Base


class ErpStaffLeaveRestriction(Base):
    """Local projection of ERP-owned staff leave write restrictions."""

    __tablename__ = "erp_staff_leave_restrictions"
    __table_args__ = (
        UniqueConstraint(
            "source_system",
            "restriction_id",
            name="uq_erp_staff_leave_restriction_identity",
        ),
        CheckConstraint(
            "status IN ('active', 'cancelled', 'revoked')",
            name="ck_erp_staff_leave_restrictions_status",
        ),
        CheckConstraint("version >= 1", name="ck_erp_staff_leave_restrictions_version"),
        Index(
            "ix_erp_staff_leave_restrictions_active_user",
            "system_user_id",
            "status",
            "effective_from",
            "effective_until",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    source_system: Mapped[str] = mapped_column(String(80), nullable=False)
    restriction_id: Mapped[str] = mapped_column(String(200), nullable=False)
    erp_employee_id: Mapped[str] = mapped_column(String(200), nullable=False)
    system_user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("system_users.id", ondelete="RESTRICT")
    )
    effective_from: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    effective_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    status: Mapped[str] = mapped_column(String(24), nullable=False)
    version: Mapped[int] = mapped_column(Integer, nullable=False)
    source_updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    last_event_id: Mapped[str] = mapped_column(String(240), nullable=False)
    last_delivery_id: Mapped[str | None] = mapped_column(String(240))
    last_observed_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(UTC), nullable=False
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(UTC), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(UTC),
        onupdate=lambda: datetime.now(UTC),
        nullable=False,
    )


class ErpStaffAccountStatusProjection(Base):
    """Local projection of ERP-owned employee account status."""

    __tablename__ = "erp_staff_account_status_projections"
    __table_args__ = (
        UniqueConstraint(
            "source_system",
            "erp_employee_id",
            name="uq_erp_staff_account_status_employee",
        ),
        CheckConstraint(
            "desired_status IN ('active', 'inactive')",
            name="ck_erp_staff_account_status_desired",
        ),
        CheckConstraint("version >= 1", name="ck_erp_staff_account_status_version"),
        Index(
            "ix_erp_staff_account_status_user",
            "system_user_id",
            "desired_status",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    source_system: Mapped[str] = mapped_column(String(80), nullable=False)
    erp_employee_id: Mapped[str] = mapped_column(String(200), nullable=False)
    system_user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("system_users.id", ondelete="RESTRICT")
    )
    desired_status: Mapped[str] = mapped_column(String(24), nullable=False)
    version: Mapped[int] = mapped_column(Integer, nullable=False)
    source_updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    reason: Mapped[str | None] = mapped_column(String(240))
    last_event_id: Mapped[str] = mapped_column(String(240), nullable=False)
    last_delivery_id: Mapped[str | None] = mapped_column(String(240))
    erp_inactive_applied: Mapped[bool] = mapped_column(
        Boolean, default=False, nullable=False
    )
    erp_inactive_applied_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True)
    )
    last_observed_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(UTC), nullable=False
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(UTC), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(UTC),
        onupdate=lambda: datetime.now(UTC),
        nullable=False,
    )
