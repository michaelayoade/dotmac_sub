"""Payment arrangement models for customer payment plans."""

import enum
import uuid
from datetime import date, datetime, timezone
from decimal import Decimal

from sqlalchemy import Boolean, Date, DateTime, Enum, ForeignKey, Integer, Numeric, String, Text
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db import Base


class PaymentFrequency(enum.Enum):
    """Frequency options for payment arrangements."""
    weekly = "weekly"
    biweekly = "biweekly"
    monthly = "monthly"


class ArrangementStatus(enum.Enum):
    """Status for payment arrangements."""
    pending = "pending"      # Requested, awaiting approval
    active = "active"        # Approved and in progress
    completed = "completed"  # All installments paid
    defaulted = "defaulted"  # Missed payments, arrangement failed
    canceled = "canceled"    # Canceled by user or admin


class InstallmentStatus(enum.Enum):
    """Status for individual installments."""
    pending = "pending"      # Not yet due
    due = "due"              # Currently due
    paid = "paid"            # Payment received
    overdue = "overdue"      # Past due, not paid
    waived = "waived"        # Waived by admin


class PaymentArrangement(Base):
    """Payment arrangement allowing customers to pay in installments.

    Can be linked to a specific invoice or the entire account balance.
    """
    __tablename__ = "payment_arrangements"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    account_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("subscriber_accounts.id"), nullable=False
    )
    invoice_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("invoices.id")
    )
    total_amount: Mapped[Decimal] = mapped_column(Numeric(12, 2), nullable=False)
    installment_amount: Mapped[Decimal] = mapped_column(Numeric(12, 2), nullable=False)
    frequency: Mapped[PaymentFrequency] = mapped_column(
        Enum(PaymentFrequency), default=PaymentFrequency.monthly
    )
    installments_total: Mapped[int] = mapped_column(Integer, nullable=False)
    installments_paid: Mapped[int] = mapped_column(Integer, default=0)
    start_date: Mapped[date] = mapped_column(Date, nullable=False)
    end_date: Mapped[date | None] = mapped_column(Date)
    next_due_date: Mapped[date | None] = mapped_column(Date)
    status: Mapped[ArrangementStatus] = mapped_column(
        Enum(ArrangementStatus), default=ArrangementStatus.pending
    )
    requested_by_person_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("people.id")
    )
    approved_by_person_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("people.id")
    )
    approved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    notes: Mapped[str | None] = mapped_column(Text)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(timezone.utc)
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(timezone.utc),
        onupdate=lambda: datetime.now(timezone.utc),
    )

    account = relationship("SubscriberAccount")
    invoice = relationship("Invoice")
    requested_by = relationship("Person", foreign_keys=[requested_by_person_id])
    approved_by = relationship("Person", foreign_keys=[approved_by_person_id])
    installments = relationship(
        "PaymentArrangementInstallment",
        back_populates="arrangement",
        cascade="all, delete-orphan",
    )


class PaymentArrangementInstallment(Base):
    """Individual installment within a payment arrangement."""
    __tablename__ = "payment_arrangement_installments"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    arrangement_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("payment_arrangements.id"), nullable=False
    )
    installment_number: Mapped[int] = mapped_column(Integer, nullable=False)
    amount: Mapped[Decimal] = mapped_column(Numeric(12, 2), nullable=False)
    due_date: Mapped[date] = mapped_column(Date, nullable=False)
    paid_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    payment_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("payments.id")
    )
    status: Mapped[InstallmentStatus] = mapped_column(
        Enum(InstallmentStatus), default=InstallmentStatus.pending
    )
    notes: Mapped[str | None] = mapped_column(Text)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(timezone.utc)
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(timezone.utc),
        onupdate=lambda: datetime.now(timezone.utc),
    )

    arrangement = relationship("PaymentArrangement", back_populates="installments")
    payment = relationship("Payment")
