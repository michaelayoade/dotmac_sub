import uuid
from datetime import UTC, datetime
from decimal import Decimal

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Numeric,
    String,
    Text,
    text,
)
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db import Base


class CutoverBalanceVariance(Base):
    __tablename__ = "cutover_balance_variances"
    __table_args__ = (
        CheckConstraint(
            "direction IN ('overcredited', 'understated')",
            name="ck_cutover_balance_variances_direction",
        ),
        CheckConstraint(
            "status IN ('accepted', 'superseded', 'rejected')",
            name="ck_cutover_balance_variances_status",
        ),
        Index(
            "uq_cutover_balance_variances_active_account",
            "account_id",
            unique=True,
            postgresql_where=text("is_active AND status = 'accepted'"),
        ),
        Index("ix_cutover_balance_variances_account_id", "account_id"),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    account_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("subscribers.id"), nullable=False
    )
    expected_drift: Mapped[Decimal] = mapped_column(Numeric(12, 2), nullable=False)
    direction: Mapped[str] = mapped_column(String(20), nullable=False)
    reason: Mapped[str] = mapped_column(String(120), nullable=False)
    evidence_ref: Mapped[str] = mapped_column(Text, nullable=False)
    approved_by: Mapped[str] = mapped_column(String(120), nullable=False)
    approved_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(UTC), nullable=False
    )
    status: Mapped[str] = mapped_column(String(20), default="accepted", nullable=False)
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

    account = relationship("Subscriber")
