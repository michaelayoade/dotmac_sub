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
    JSON,
    Boolean,
    CheckConstraint,
    DateTime,
    Enum,
    ForeignKey,
    Index,
    Integer,
    Numeric,
    String,
    Text,
    UniqueConstraint,
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


class VasTransactionStatus(enum.Enum):
    pending = "pending"
    debited = "debited"
    submitted = "submitted"
    delivered = "delivered"
    failed = "failed"
    refunded = "refunded"
    review = "review"


class VasRefundStatus(enum.Enum):
    """Durable refund-to-source lifecycle owned by VAS reconciliation."""

    prepared = "prepared"
    submitting = "submitting"
    accepted = "accepted"
    succeeded = "succeeded"
    failed = "failed"
    needs_attention = "needs_attention"


class VasRefundRequest(Base):
    """One durable refund-to-source request per wallet top-up.

    The wallet debit and this row are committed together before a gateway is
    called. Gateway observations then advance this row, while terminal failure
    creates one explicit compensating wallet credit. This makes replay and
    crash recovery converge on persisted state rather than an HTTP response.
    """

    __tablename__ = "vas_refund_requests"
    __table_args__ = (
        CheckConstraint("amount > 0", name="ck_vas_refund_requests_amount_positive"),
        UniqueConstraint(
            "provider",
            "provider_refund_id",
            name="uq_vas_refund_requests_provider_refund",
        ),
        Index(
            "ix_vas_refund_requests_status_updated",
            "status",
            "updated_at",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    topup_entry_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("vas_wallet_entries.id"),
        nullable=False,
        unique=True,
    )
    wallet_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("vas_wallets.id"), nullable=False
    )
    wallet_debit_entry_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("vas_wallet_entries.id"), unique=True
    )
    wallet_reversal_entry_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("vas_wallet_entries.id"), unique=True
    )
    provider: Mapped[str] = mapped_column(String(40), nullable=False)
    funding_reference: Mapped[str] = mapped_column(String(120), nullable=False)
    provider_transaction_id: Mapped[str | None] = mapped_column(String(120))
    provider_refund_id: Mapped[str | None] = mapped_column(String(120))
    amount: Mapped[Decimal] = mapped_column(Numeric(12, 2), nullable=False)
    currency: Mapped[str] = mapped_column(String(3), nullable=False, default="NGN")
    status: Mapped[VasRefundStatus] = mapped_column(
        Enum(VasRefundStatus, native_enum=False, length=32),
        nullable=False,
        default=VasRefundStatus.prepared,
    )
    provider_status: Mapped[str | None] = mapped_column(String(80))
    provider_response: Mapped[dict | None] = mapped_column(JSON)
    submit_attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    reconcile_attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    last_error: Mapped[str | None] = mapped_column(Text)
    submitted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_reconciled_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(UTC)
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(UTC),
        onupdate=lambda: datetime.now(UTC),
    )

    topup_entry = relationship("VasWalletEntry", foreign_keys=[topup_entry_id])
    wallet = relationship("VasWallet")
    wallet_debit_entry = relationship(
        "VasWalletEntry", foreign_keys=[wallet_debit_entry_id]
    )
    wallet_reversal_entry = relationship(
        "VasWalletEntry", foreign_keys=[wallet_reversal_entry_id]
    )


class VasService(Base):
    """A purchasable VTPass service (synced from their catalog)."""

    __tablename__ = "vas_services"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    category: Mapped[str] = mapped_column(String(60), nullable=False, index=True)
    service_id: Mapped[str] = mapped_column(String(120), unique=True, nullable=False)
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    image_url: Mapped[str | None] = mapped_column(String(400))
    identifier_label: Mapped[str | None] = mapped_column(String(120))
    requires_verify: Mapped[bool] = mapped_column(Boolean, default=False)
    is_enabled: Mapped[bool] = mapped_column(Boolean, default=False)
    min_amount: Mapped[Decimal | None] = mapped_column(Numeric(12, 2))
    max_amount: Mapped[Decimal | None] = mapped_column(Numeric(12, 2))
    raw: Mapped[dict | None] = mapped_column("raw", JSON)
    synced_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(UTC)
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(UTC),
        onupdate=lambda: datetime.now(UTC),
    )

    variations = relationship("VasServiceVariation", back_populates="service")


class VasServiceVariation(Base):
    __tablename__ = "vas_service_variations"
    __table_args__ = (
        UniqueConstraint(
            "service_pk", "code", name="uq_vas_service_variations_service_code"
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    service_pk: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("vas_services.id"), nullable=False
    )
    code: Mapped[str] = mapped_column(String(120), nullable=False)
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    amount: Mapped[Decimal | None] = mapped_column(Numeric(12, 2))
    is_enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    raw: Mapped[dict | None] = mapped_column("raw", JSON)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(UTC)
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(UTC),
        onupdate=lambda: datetime.now(UTC),
    )

    service = relationship("VasService", back_populates="variations")


class VasTransaction(Base):
    """A bill-payment purchase, driven by the requery-trusting state machine.

    pending -> debited -> submitted -> delivered
                                    -> failed -> refunded
    submitted past the requery cap  -> review (manual queue; terminal states
    are monotonic — late provider confirmations must never flip refunded).
    """

    __tablename__ = "vas_transactions"
    __table_args__ = (
        Index("ix_vas_transactions_wallet_created", "wallet_id", "created_at"),
        Index("ix_vas_transactions_status", "status"),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    wallet_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("vas_wallets.id"), nullable=False
    )
    subscriber_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("subscribers.id")
    )
    # Initiator attribution, set at purchase time from the wallet owner —
    # NULL for customer-direct. Designed in ahead of Phase 3 (reseller
    # commissions) so no backfill is ever needed.
    reseller_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("resellers.id")
    )
    service_pk: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("vas_services.id"), nullable=False
    )
    variation_code: Mapped[str | None] = mapped_column(String(120))
    identifier: Mapped[str] = mapped_column(String(120), nullable=False)
    amount: Mapped[Decimal] = mapped_column(Numeric(12, 2), nullable=False)
    request_id: Mapped[str] = mapped_column(String(120), unique=True, nullable=False)
    status: Mapped[VasTransactionStatus] = mapped_column(
        Enum(VasTransactionStatus),
        default=VasTransactionStatus.pending,
        nullable=False,
    )
    requery_attempts: Mapped[int] = mapped_column(Integer, default=0)
    # Rate snapshot at time of sale (Phase 3 fills these; rate cards answer
    # "what's the rate now", the snapshot answers "what was THIS priced at").
    vtpass_rate_pct: Mapped[Decimal | None] = mapped_column(Numeric(7, 4))
    reseller_rate_pct: Mapped[Decimal | None] = mapped_column(Numeric(7, 4))
    owner_net: Mapped[Decimal | None] = mapped_column(Numeric(12, 2))
    provider_status: Mapped[str | None] = mapped_column(String(120))
    provider_response: Mapped[dict | None] = mapped_column(JSON)
    # Encrypted at rest (credential_crypto); written BEFORE delivered.
    token_encrypted: Mapped[str | None] = mapped_column(Text)
    error: Mapped[str | None] = mapped_column(Text)
    delivered_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    refunded_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(UTC)
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(UTC),
        onupdate=lambda: datetime.now(UTC),
    )

    wallet = relationship("VasWallet")
    service = relationship("VasService")


class VasPartyType(enum.Enum):
    owner = "owner"
    reseller = "reseller"


class VasRateCard(Base):
    """Effective-dated commission rates per category and party.

    The owner row tracks our VTPass rate as volume tiers us up; reseller rows
    are the fixed rates locked per agreement (deferred-override model: the
    spread owner_rate - reseller_rate is the platform margin and is NEVER
    exposed to resellers). Rates answer "what's the rate now"; each
    transaction snapshots what IT was priced at.
    """

    __tablename__ = "vas_rate_cards"
    __table_args__ = (
        Index("ix_vas_rate_cards_lookup", "category", "party_type", "effective_from"),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    category: Mapped[str] = mapped_column(String(60), nullable=False)
    party_type: Mapped[VasPartyType] = mapped_column(Enum(VasPartyType), nullable=False)
    rate_pct: Mapped[Decimal] = mapped_column(Numeric(7, 4), nullable=False)
    effective_from: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    memo: Mapped[str | None] = mapped_column(Text)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(UTC)
    )


class VasTopupIntent(Base):
    """Binds a gateway checkout reference to the wallet that initiated it.

    Without this, any authenticated user who learns someone else's checkout
    reference could verify it into their OWN wallet (reference theft). The
    intent row is written at initiate; verify requires it and the wallet to
    match.
    """

    __tablename__ = "vas_topup_intents"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    reference: Mapped[str] = mapped_column(String(120), unique=True, nullable=False)
    wallet_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("vas_wallets.id"), nullable=False
    )
    amount: Mapped[Decimal] = mapped_column(Numeric(12, 2), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(UTC)
    )
