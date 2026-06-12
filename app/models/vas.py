"""VAS (bill-payments) wallet models.

Deliberately separate from the billing ledger: ``ledger_entries`` is service
money that collections/enforcement read (credit − open invoices). Wallet
balances are customer liabilities and must never be visible to that math —
money only crosses over as a normal ``Payment`` when the customer pays their
DotMac bill from the wallet (see docs/designs/VTU_BILL_PAYMENTS.md).
"""

import enum
import uuid
from datetime import UTC, datetime
from decimal import Decimal

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    DateTime,
    Enum,
    ForeignKey,
    Index,
    Numeric,
    String,
    Text,
)
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db import Base


class VasEntryType(enum.Enum):
    credit = "credit"
    debit = "debit"


class VasEntryCategory(enum.Enum):
    topup = "topup"
    purchase = "purchase"
    purchase_refund = "purchase_refund"
    bill_payment = "bill_payment"
    commission = "commission"
    adjustment = "adjustment"


class VasWallet(Base):
    """One wallet per subscriber (or reseller float wallet — Phase 3)."""

    __tablename__ = "vas_wallets"
    __table_args__ = (
        CheckConstraint(
            "(subscriber_id IS NOT NULL AND reseller_id IS NULL)"
            " OR (subscriber_id IS NULL AND reseller_id IS NOT NULL)",
            name="ck_vas_wallets_exactly_one_owner",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    subscriber_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("subscribers.id"), unique=True
    )
    reseller_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("resellers.id"), unique=True
    )
    auto_pay_bill_enabled: Mapped[bool] = mapped_column(Boolean, default=False)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(UTC)
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(UTC),
        onupdate=lambda: datetime.now(UTC),
    )

    subscriber = relationship("Subscriber")
    entries = relationship("VasWalletEntry", back_populates="wallet")


class VasWalletEntry(Base):
    """Append-only wallet ledger. Balance = Σ credits − Σ debits.

    Debits are taken immediately (purchase time), refunds are explicit credit
    entries — there is no separate holds concept; under
    ``billing/_common.lock_account`` serialization the balance itself is the
    spendable amount.
    """

    __tablename__ = "vas_wallet_entries"
    __table_args__ = (
        CheckConstraint("amount > 0", name="ck_vas_wallet_entries_amount_positive"),
        Index("ix_vas_wallet_entries_wallet_created", "wallet_id", "created_at"),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    wallet_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("vas_wallets.id"), nullable=False
    )
    entry_type: Mapped[VasEntryType] = mapped_column(Enum(VasEntryType), nullable=False)
    category: Mapped[VasEntryCategory] = mapped_column(
        Enum(VasEntryCategory), nullable=False
    )
    amount: Mapped[Decimal] = mapped_column(Numeric(12, 2), nullable=False)
    currency: Mapped[str] = mapped_column(String(3), default="NGN")
    # Gateway/transaction reference — unique where present (idempotency anchor
    # for top-up verification and purchase linkage).
    reference: Mapped[str | None] = mapped_column(String(120), unique=True)
    payment_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("payments.id")
    )
    memo: Mapped[str | None] = mapped_column(Text)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(UTC)
    )

    wallet = relationship("VasWallet", back_populates="entries")
    payment = relationship("Payment")
