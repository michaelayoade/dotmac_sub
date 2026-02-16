import enum
import uuid
from datetime import datetime, timezone
from decimal import Decimal

from sqlalchemy import (
    Boolean,
    DateTime,
    Enum,
    ForeignKey,
    Integer,
    JSON,
    Numeric,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db import Base


class InvoiceStatus(enum.Enum):
    draft = "draft"
    issued = "issued"
    partially_paid = "partially_paid"
    paid = "paid"
    void = "void"
    overdue = "overdue"


class BillingRunStatus(enum.Enum):
    running = "running"
    success = "success"
    failed = "failed"


class PaymentMethodType(enum.Enum):
    card = "card"
    bank_account = "bank_account"
    cash = "cash"
    check = "check"
    transfer = "transfer"
    other = "other"


class PaymentStatus(enum.Enum):
    pending = "pending"
    succeeded = "succeeded"
    failed = "failed"
    refunded = "refunded"
    partially_refunded = "partially_refunded"
    canceled = "canceled"


class PaymentProviderType(enum.Enum):
    stripe = "stripe"
    paypal = "paypal"
    manual = "manual"
    custom = "custom"


class PaymentProviderEventStatus(enum.Enum):
    pending = "pending"
    processed = "processed"
    failed = "failed"


class LedgerEntryType(enum.Enum):
    debit = "debit"
    credit = "credit"


class LedgerSource(enum.Enum):
    invoice = "invoice"
    payment = "payment"
    adjustment = "adjustment"
    refund = "refund"
    credit_note = "credit_note"
    other = "other"


class TaxApplication(enum.Enum):
    exclusive = "exclusive"
    inclusive = "inclusive"
    exempt = "exempt"


class BankAccountType(enum.Enum):
    checking = "checking"
    savings = "savings"
    business = "business"
    other = "other"


class PaymentChannelType(enum.Enum):
    card = "card"
    bank_transfer = "bank_transfer"
    cash = "cash"
    check = "check"
    transfer = "transfer"
    other = "other"


class CollectionAccountType(enum.Enum):
    bank = "bank"
    cash = "cash"
    other = "other"


class Invoice(Base):
    __tablename__ = "invoices"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    account_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("subscribers.id"), nullable=False
    )
    invoice_number: Mapped[str | None] = mapped_column(String(80))
    status: Mapped[InvoiceStatus] = mapped_column(
        Enum(InvoiceStatus), default=InvoiceStatus.draft
    )
    currency: Mapped[str] = mapped_column(String(3), default="NGN")
    subtotal: Mapped[Decimal] = mapped_column(Numeric(12, 2), default=Decimal("0.00"))
    tax_total: Mapped[Decimal] = mapped_column(Numeric(12, 2), default=Decimal("0.00"))
    total: Mapped[Decimal] = mapped_column(Numeric(12, 2), default=Decimal("0.00"))
    balance_due: Mapped[Decimal] = mapped_column(
        Numeric(12, 2), default=Decimal("0.00")
    )
    billing_period_start: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    billing_period_end: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    issued_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    due_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    paid_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    memo: Mapped[str | None] = mapped_column(Text)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(timezone.utc)
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(timezone.utc), onupdate=lambda: datetime.now(timezone.utc)
    )

    account = relationship("Subscriber")
    lines = relationship("InvoiceLine", back_populates="invoice")
    payment_allocations = relationship("PaymentAllocation", back_populates="invoice")
    ledger_entries = relationship("LedgerEntry", back_populates="invoice")
    dunning_actions = relationship("DunningActionLog", back_populates="invoice")
    credit_note_applications = relationship(
        "CreditNoteApplication", back_populates="invoice"
    )


class CreditNoteStatus(enum.Enum):
    draft = "draft"
    issued = "issued"
    partially_applied = "partially_applied"
    applied = "applied"
    void = "void"


class CreditNote(Base):
    __tablename__ = "credit_notes"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    account_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("subscribers.id"), nullable=False
    )
    invoice_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("invoices.id")
    )
    credit_number: Mapped[str | None] = mapped_column(String(80))
    status: Mapped[CreditNoteStatus] = mapped_column(
        Enum(CreditNoteStatus), default=CreditNoteStatus.draft
    )
    currency: Mapped[str] = mapped_column(String(3), default="NGN")
    subtotal: Mapped[Decimal] = mapped_column(Numeric(12, 2), default=Decimal("0.00"))
    tax_total: Mapped[Decimal] = mapped_column(Numeric(12, 2), default=Decimal("0.00"))
    total: Mapped[Decimal] = mapped_column(Numeric(12, 2), default=Decimal("0.00"))
    applied_total: Mapped[Decimal] = mapped_column(
        Numeric(12, 2), default=Decimal("0.00")
    )
    memo: Mapped[str | None] = mapped_column(Text)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(timezone.utc)
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(timezone.utc),
        onupdate=lambda: datetime.now(timezone.utc),
    )

    account = relationship("Subscriber")
    invoice = relationship("Invoice")
    lines = relationship("CreditNoteLine", back_populates="credit_note")
    applications = relationship("CreditNoteApplication", back_populates="credit_note")


class CreditNoteLine(Base):
    __tablename__ = "credit_note_lines"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    credit_note_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("credit_notes.id"), nullable=False
    )
    description: Mapped[str] = mapped_column(String(255), nullable=False)
    quantity: Mapped[Decimal] = mapped_column(Numeric(12, 3), default=Decimal("1.000"))
    unit_price: Mapped[Decimal] = mapped_column(Numeric(12, 2), default=Decimal("0.00"))
    amount: Mapped[Decimal] = mapped_column(Numeric(12, 2), default=Decimal("0.00"))
    tax_rate_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("tax_rates.id")
    )
    tax_application: Mapped[TaxApplication] = mapped_column(
        Enum(TaxApplication), default=TaxApplication.exclusive
    )
    metadata_: Mapped[str | None] = mapped_column("metadata", Text)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(timezone.utc)
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(timezone.utc),
        onupdate=lambda: datetime.now(timezone.utc),
    )

    credit_note = relationship("CreditNote", back_populates="lines")
    tax_rate = relationship("TaxRate")


class CreditNoteApplication(Base):
    __tablename__ = "credit_note_applications"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    credit_note_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("credit_notes.id"), nullable=False
    )
    invoice_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("invoices.id"), nullable=False
    )
    amount: Mapped[Decimal] = mapped_column(Numeric(12, 2), default=Decimal("0.00"))
    memo: Mapped[str | None] = mapped_column(Text)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(timezone.utc)
    )

    credit_note = relationship("CreditNote", back_populates="applications")
    invoice = relationship("Invoice", back_populates="credit_note_applications")


class InvoiceLine(Base):
    __tablename__ = "invoice_lines"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    invoice_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("invoices.id"), nullable=False
    )
    subscription_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("subscriptions.id")
    )
    description: Mapped[str] = mapped_column(String(255), nullable=False)
    quantity: Mapped[Decimal] = mapped_column(Numeric(12, 3), default=Decimal("1.000"))
    unit_price: Mapped[Decimal] = mapped_column(Numeric(12, 2), default=Decimal("0.00"))
    amount: Mapped[Decimal] = mapped_column(Numeric(12, 2), default=Decimal("0.00"))
    tax_rate_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("tax_rates.id")
    )
    tax_application: Mapped[TaxApplication] = mapped_column(
        Enum(TaxApplication), default=TaxApplication.exclusive
    )
    metadata_: Mapped[str | None] = mapped_column("metadata", Text)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(timezone.utc)
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(timezone.utc), onupdate=lambda: datetime.now(timezone.utc)
    )

    invoice = relationship("Invoice", back_populates="lines")
    tax_rate = relationship("TaxRate")


class PaymentMethod(Base):
    __tablename__ = "payment_methods"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    account_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("subscribers.id"), nullable=False
    )
    payment_channel_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("payment_channels.id")
    )
    method_type: Mapped[PaymentMethodType] = mapped_column(
        Enum(PaymentMethodType), default=PaymentMethodType.card
    )
    label: Mapped[str | None] = mapped_column(String(120))
    token: Mapped[str | None] = mapped_column(String(255))
    last4: Mapped[str | None] = mapped_column(String(4))
    brand: Mapped[str | None] = mapped_column(String(40))
    expires_month: Mapped[int | None] = mapped_column(Integer)
    expires_year: Mapped[int | None] = mapped_column(Integer)
    is_default: Mapped[bool] = mapped_column(Boolean, default=False)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(timezone.utc)
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(timezone.utc), onupdate=lambda: datetime.now(timezone.utc)
    )

    account = relationship("Subscriber")
    payment_channel = relationship("PaymentChannel", back_populates="payment_methods")
    payments = relationship("Payment", back_populates="payment_method")


class BankAccount(Base):
    __tablename__ = "bank_accounts"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    account_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("subscribers.id"), nullable=False
    )
    payment_method_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("payment_methods.id")
    )
    bank_name: Mapped[str | None] = mapped_column(String(120))
    account_type: Mapped[BankAccountType] = mapped_column(
        Enum(BankAccountType), default=BankAccountType.checking
    )
    account_last4: Mapped[str | None] = mapped_column(String(4))
    routing_last4: Mapped[str | None] = mapped_column(String(4))
    token: Mapped[str | None] = mapped_column(String(255))
    is_default: Mapped[bool] = mapped_column(Boolean, default=False)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(timezone.utc)
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(timezone.utc), onupdate=lambda: datetime.now(timezone.utc)
    )

    account = relationship("Subscriber")
    payment_method = relationship("PaymentMethod")


class Payment(Base):
    __tablename__ = "payments"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    account_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("subscribers.id"), nullable=False
    )
    payment_method_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("payment_methods.id")
    )
    payment_channel_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("payment_channels.id")
    )
    collection_account_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("collection_accounts.id")
    )
    provider_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("payment_providers.id")
    )
    amount: Mapped[Decimal] = mapped_column(Numeric(12, 2), default=Decimal("0.00"))
    currency: Mapped[str] = mapped_column(String(3), default="NGN")
    status: Mapped[PaymentStatus] = mapped_column(
        Enum(PaymentStatus), default=PaymentStatus.pending
    )
    paid_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    external_id: Mapped[str | None] = mapped_column(String(120))
    memo: Mapped[str | None] = mapped_column(Text)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(timezone.utc)
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(timezone.utc), onupdate=lambda: datetime.now(timezone.utc)
    )

    account = relationship("Subscriber")
    payment_method = relationship("PaymentMethod", back_populates="payments")
    payment_channel = relationship("PaymentChannel", back_populates="payments")
    collection_account = relationship("CollectionAccount", back_populates="payments")
    provider = relationship("PaymentProvider", back_populates="payments")
    provider_events = relationship("PaymentProviderEvent", back_populates="payment")
    ledger_entries = relationship("LedgerEntry", back_populates="payment")
    dunning_actions = relationship("DunningActionLog", back_populates="payment")
    allocations = relationship("PaymentAllocation", back_populates="payment")


class PaymentAllocation(Base):
    __tablename__ = "payment_allocations"
    __table_args__ = (
        UniqueConstraint("payment_id", "invoice_id", name="uq_payment_allocations_payment_invoice"),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    payment_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("payments.id"), nullable=False
    )
    invoice_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("invoices.id"), nullable=False
    )
    amount: Mapped[Decimal] = mapped_column(Numeric(12, 2), default=Decimal("0.00"))
    memo: Mapped[str | None] = mapped_column(Text)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(timezone.utc)
    )

    payment = relationship("Payment", back_populates="allocations")
    invoice = relationship("Invoice")


class LedgerEntry(Base):
    __tablename__ = "ledger_entries"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    account_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("subscribers.id"), nullable=False
    )
    invoice_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("invoices.id")
    )
    payment_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("payments.id")
    )
    entry_type: Mapped[LedgerEntryType] = mapped_column(
        Enum(LedgerEntryType), nullable=False
    )
    source: Mapped[LedgerSource] = mapped_column(Enum(LedgerSource))
    amount: Mapped[Decimal] = mapped_column(Numeric(12, 2), default=Decimal("0.00"))
    currency: Mapped[str] = mapped_column(String(3), default="NGN")
    memo: Mapped[str | None] = mapped_column(Text)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(timezone.utc)
    )

    account = relationship("Subscriber")
    invoice = relationship("Invoice", back_populates="ledger_entries")
    payment = relationship("Payment", back_populates="ledger_entries")


class TaxRate(Base):
    __tablename__ = "tax_rates"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    name: Mapped[str] = mapped_column(String(120), nullable=False)
    code: Mapped[str | None] = mapped_column(String(40))
    rate: Mapped[Decimal] = mapped_column(Numeric(6, 4), default=Decimal("0.0000"))
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    description: Mapped[str | None] = mapped_column(Text)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(timezone.utc)
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(timezone.utc), onupdate=lambda: datetime.now(timezone.utc)
    )


class PaymentProvider(Base):
    __tablename__ = "payment_providers"
    __table_args__ = (UniqueConstraint("name", name="uq_payment_providers_name"),)

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    name: Mapped[str] = mapped_column(String(160), nullable=False)
    provider_type: Mapped[PaymentProviderType] = mapped_column(
        Enum(PaymentProviderType), default=PaymentProviderType.custom
    )
    connector_config_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("connector_configs.id")
    )
    webhook_secret_ref: Mapped[str | None] = mapped_column(String(255))
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    notes: Mapped[str | None] = mapped_column(Text)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(timezone.utc)
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(timezone.utc), onupdate=lambda: datetime.now(timezone.utc)
    )

    connector_config = relationship("ConnectorConfig")
    payments = relationship("Payment", back_populates="provider")
    events = relationship("PaymentProviderEvent", back_populates="provider")


class CollectionAccount(Base):
    __tablename__ = "collection_accounts"
    __table_args__ = (UniqueConstraint("name", name="uq_collection_accounts_name"),)

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    name: Mapped[str] = mapped_column(String(160), nullable=False)
    account_type: Mapped[CollectionAccountType] = mapped_column(
        Enum(CollectionAccountType), default=CollectionAccountType.bank
    )
    bank_name: Mapped[str | None] = mapped_column(String(120))
    account_last4: Mapped[str | None] = mapped_column(String(4))
    currency: Mapped[str] = mapped_column(String(3), default="NGN")
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    notes: Mapped[str | None] = mapped_column(Text)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(timezone.utc)
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(timezone.utc),
        onupdate=lambda: datetime.now(timezone.utc),
    )

    payments = relationship("Payment", back_populates="collection_account")
    channel_mappings = relationship("PaymentChannelAccount", back_populates="collection_account")


class PaymentChannel(Base):
    __tablename__ = "payment_channels"
    __table_args__ = (UniqueConstraint("name", name="uq_payment_channels_name"),)

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    name: Mapped[str] = mapped_column(String(160), nullable=False)
    channel_type: Mapped[PaymentChannelType] = mapped_column(
        Enum(PaymentChannelType), default=PaymentChannelType.other
    )
    provider_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("payment_providers.id")
    )
    default_collection_account_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("collection_accounts.id")
    )
    fee_rules: Mapped[dict | None] = mapped_column(JSON)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    is_default: Mapped[bool] = mapped_column(Boolean, default=False)
    notes: Mapped[str | None] = mapped_column(Text)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(timezone.utc)
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(timezone.utc),
        onupdate=lambda: datetime.now(timezone.utc),
    )

    provider = relationship("PaymentProvider")
    default_collection_account = relationship("CollectionAccount")
    channel_accounts = relationship("PaymentChannelAccount", back_populates="channel")
    payments = relationship("Payment", back_populates="payment_channel")
    payment_methods = relationship("PaymentMethod", back_populates="payment_channel")


class PaymentChannelAccount(Base):
    __tablename__ = "payment_channel_accounts"
    __table_args__ = (
        UniqueConstraint(
            "channel_id",
            "collection_account_id",
            "currency",
            name="uq_payment_channel_accounts_channel_account_currency",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    channel_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("payment_channels.id"), nullable=False
    )
    collection_account_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("collection_accounts.id"), nullable=False
    )
    currency: Mapped[str | None] = mapped_column(String(3))
    priority: Mapped[int] = mapped_column(Integer, default=0)
    is_default: Mapped[bool] = mapped_column(Boolean, default=False)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(timezone.utc)
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(timezone.utc),
        onupdate=lambda: datetime.now(timezone.utc),
    )

    channel = relationship("PaymentChannel", back_populates="channel_accounts")
    collection_account = relationship("CollectionAccount", back_populates="channel_mappings")


class PaymentProviderEvent(Base):
    __tablename__ = "payment_provider_events"
    __table_args__ = (
        UniqueConstraint(
            "provider_id",
            "idempotency_key",
            name="uq_payment_provider_events_idempotency",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    provider_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("payment_providers.id"), nullable=False
    )
    payment_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("payments.id")
    )
    invoice_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("invoices.id")
    )
    event_type: Mapped[str] = mapped_column(String(120), nullable=False)
    external_id: Mapped[str | None] = mapped_column(String(160))
    idempotency_key: Mapped[str | None] = mapped_column(String(160))
    status: Mapped[PaymentProviderEventStatus] = mapped_column(
        Enum(PaymentProviderEventStatus), default=PaymentProviderEventStatus.pending
    )
    payload: Mapped[dict | None] = mapped_column(JSON)
    error: Mapped[str | None] = mapped_column(Text)
    received_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(timezone.utc)
    )
    processed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    provider = relationship("PaymentProvider", back_populates="events")
    payment = relationship("Payment", back_populates="provider_events")
    invoice = relationship("Invoice")


class BillingRun(Base):
    __tablename__ = "billing_runs"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    run_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    billing_cycle: Mapped[str | None] = mapped_column(String(40))
    status: Mapped[BillingRunStatus] = mapped_column(
        Enum(BillingRunStatus), default=BillingRunStatus.running
    )
    started_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(timezone.utc)
    )
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    subscriptions_scanned: Mapped[int] = mapped_column(Integer, default=0)
    subscriptions_billed: Mapped[int] = mapped_column(Integer, default=0)
    invoices_created: Mapped[int] = mapped_column(Integer, default=0)
    lines_created: Mapped[int] = mapped_column(Integer, default=0)
    skipped: Mapped[int] = mapped_column(Integer, default=0)
    error: Mapped[str | None] = mapped_column(Text)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(timezone.utc)
    )
