"""Bank-transfer payment proofs.

Customers (or their reseller) pay by direct bank transfer and upload the
receipt; staff verify and the amount is credited to the account as a real
Payment (method=transfer) — optionally auto-allocated to the oldest open
invoices — through the same billing rails as every other payment.
"""

import enum
import uuid
from datetime import UTC, datetime
from decimal import Decimal

from sqlalchemy import DateTime, Enum, ForeignKey, Numeric, String, Text
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db import Base


class PaymentProofStatus(enum.Enum):
    submitted = "submitted"
    verified = "verified"
    rejected = "rejected"


class PaymentProof(Base):
    __tablename__ = "payment_proofs"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    account_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("subscribers.id"), nullable=False, index=True
    )
    # Who uploaded it — the customer themselves or a reseller user.
    submitted_by: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("subscribers.id"), nullable=True
    )
    amount: Mapped[Decimal] = mapped_column(Numeric(12, 2), nullable=False)
    currency: Mapped[str] = mapped_column(String(3), default="NGN")
    bank_name: Mapped[str | None] = mapped_column(String(120))
    reference: Mapped[str | None] = mapped_column(String(160))
    paid_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    file_path: Mapped[str] = mapped_column(String(500), nullable=False)
    status: Mapped[PaymentProofStatus] = mapped_column(
        Enum(PaymentProofStatus),
        default=PaymentProofStatus.submitted,
        nullable=False,
        index=True,
    )
    review_notes: Mapped[str | None] = mapped_column(Text)
    verified_by: Mapped[str | None] = mapped_column(String(120))
    payment_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("payments.id"), nullable=True
    )

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(UTC)
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(UTC),
        onupdate=lambda: datetime.now(UTC),
    )

    account = relationship("Subscriber", foreign_keys=[account_id])
    payment = relationship("Payment")
