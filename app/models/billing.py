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
    text,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db import Base


class InvoiceStatus(enum.Enum):
    draft = "draft"
    issued = "issued"
    partially_paid = "partially_paid"
    paid = "paid"
    void = "void"
    overdue = "overdue"
    # Closed-but-not-collected: the obligation was written off as bad debt.
    # Materially distinct from ``paid`` (obligation satisfied) and ``void``
    # (invoice should never have existed). The loss is recorded as a credit
    # adjustment in the ledger (the financial source of truth); the invoice
    # stays on record, excluded from outstanding/aging but NOT counted as cash.
    written_off = "written_off"


class InvoicePdfExportStatus(enum.Enum):
    queued = "queued"
    processing = "processing"
    completed = "completed"
    failed = "failed"


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
    paystack = "paystack"
    flutterwave = "flutterwave"
    manual = "manual"
    custom = "custom"


class PaymentProviderEventStatus(enum.Enum):
    pending = "pending"
    processed = "processed"
    failed = "failed"


class PaymentWebhookDeadLetterStatus(enum.Enum):
    # Captured on receipt, before processing was attempted. A row stuck in this
    # state means the worker died mid-ingest (crash/OOM/kill) — it is replayable.
    received = "received"
    # Ingest raised an unexpected/transient error. The provider was told to
    # retry (HTTP 5xx); this row is replayable if the provider stops retrying.
    failed = "failed"
    # Ingest deterministically rejected the payload (HTTP 4xx, e.g. bad data).
    # Replaying as-is will not help; needs human attention.
    rejected = "rejected"
    # A previously-failed event was successfully reprocessed.
    replayed = "replayed"


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


class LedgerCategory(enum.Enum):
    """What the ledger entry is for (financial reporting category)."""

    internet_service = "internet_service"
    custom_service = "custom_service"
    voice_service = "voice_service"
    bundle_service = "bundle_service"
    installation_fee = "installation_fee"
    equipment_rental = "equipment_rental"
    equipment_purchase = "equipment_purchase"
    late_payment_fee = "late_payment_fee"
    reconnection_fee = "reconnection_fee"
    deposit = "deposit"
    discount = "discount"
    tax = "tax"
    overage = "overage"
    top_up = "top_up"
    other = "other"


class ServiceEntitlementStatus(enum.Enum):
    active = "active"
    void = "void"
    reversed = "reversed"


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


class BillingAccount(Base):
    """Consolidated billing parent for a Reseller.

    One BillingAccount per Reseller. Owns consolidated payments that can be
    allocated across any invoice belonging to a Subscriber under that Reseller.
    Unallocated payment surplus is held in `balance`.
    """

    __tablename__ = "billing_accounts"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    reseller_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("resellers.id"),
        nullable=False,
        unique=True,
    )
    name: Mapped[str] = mapped_column(String(160), nullable=False)
    currency: Mapped[str] = mapped_column(String(3), default="NGN")
    status: Mapped[str] = mapped_column(String(20), default="active")
    balance: Mapped[Decimal] = mapped_column(Numeric(12, 2), default=Decimal("0.00"))
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    metadata_: Mapped[dict | None] = mapped_column("metadata", JSONB)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(UTC)
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(UTC),
        onupdate=lambda: datetime.now(UTC),
    )

    reseller = relationship("Reseller", back_populates="billing_account")
    payments = relationship("Payment", back_populates="billing_account")


class Invoice(Base):
    __tablename__ = "invoices"
    __table_args__ = (
        Index(
            "uq_invoices_active_splynx_invoice_id",
            "splynx_invoice_id",
            unique=True,
            postgresql_where=text("is_active AND splynx_invoice_id IS NOT NULL"),
        ),
        # Idempotency key for CRM-created invoices (installation charges). A
        # dedicated column, not metadata->>'crm_external_ref', so the partial
        # unique index is portable to the SQLite test suite. sqlite_where keeps
        # the predicate (else SQLite would constrain inactive/voided rows too).
        Index(
            "uq_invoices_active_crm_external_ref",
            "crm_external_ref",
            unique=True,
            postgresql_where=text("is_active AND crm_external_ref IS NOT NULL"),
            sqlite_where=text("is_active AND crm_external_ref IS NOT NULL"),
        ),
        # Backs the per-account billing list (active invoices, newest first)
        # and FK joins on account_id.
        Index(
            "ix_invoices_account_id_is_active_issued_at",
            "account_id",
            "is_active",
            "issued_at",
        ),
        # Backs the ERP AR incremental sync watermark (WHERE is_active AND
        # updated_at >= :cutoff ORDER BY updated_at). Without it the sync did a
        # global unindexed sort → long-running sessions → app pool starvation.
        Index(
            "ix_invoices_is_active_updated_at",
            "is_active",
            "updated_at",
        ),
        # Backs the un-watermarked UI default (ORDER BY created_at DESC over
        # active invoices) so that path stops sequentially sorting too.
        Index(
            "ix_invoices_is_active_created_at",
            "is_active",
            "created_at",
        ),
    )

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
    billing_period_start: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True)
    )
    billing_period_end: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    issued_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    due_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    paid_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    memo: Mapped[str | None] = mapped_column(Text)
    is_proforma: Mapped[bool] = mapped_column(Boolean, default=False)
    is_sent: Mapped[bool | None] = mapped_column(Boolean, default=False)
    added_by_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("subscribers.id")
    )
    splynx_invoice_id: Mapped[int | None] = mapped_column(Integer)
    # Idempotency key for CRM-created invoices (also mirrored into metadata for
    # back-compat reads); backs uq_invoices_active_crm_external_ref.
    crm_external_ref: Mapped[str | None] = mapped_column(String(120))
    metadata_: Mapped[dict | None] = mapped_column("metadata", JSONB)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(UTC)
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(UTC),
        onupdate=lambda: datetime.now(UTC),
    )

    account = relationship("Subscriber", foreign_keys=[account_id])
    added_by = relationship("Subscriber", foreign_keys=[added_by_id])
    lines = relationship("InvoiceLine", back_populates="invoice")
    payment_allocations = relationship("PaymentAllocation", back_populates="invoice")
    ledger_entries = relationship("LedgerEntry", back_populates="invoice")
    dunning_actions = relationship("DunningActionLog", back_populates="invoice")
    credit_note_applications = relationship(
        "CreditNoteApplication", back_populates="invoice"
    )
    pdf_exports = relationship("InvoicePdfExport", back_populates="invoice")


class InvoicePdfExport(Base):
    __tablename__ = "invoice_pdf_exports"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    invoice_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("invoices.id"), nullable=False
    )
    status: Mapped[InvoicePdfExportStatus] = mapped_column(
        Enum(InvoicePdfExportStatus, name="invoicepdfexportstatus"),
        default=InvoicePdfExportStatus.queued,
        nullable=False,
    )
    requested_by_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("subscribers.id")
    )
    celery_task_id: Mapped[str | None] = mapped_column(String(120))
    file_path: Mapped[str | None] = mapped_column(String(500))
    file_size_bytes: Mapped[int | None] = mapped_column(Integer)
    error: Mapped[str | None] = mapped_column(Text)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(UTC)
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(UTC),
        onupdate=lambda: datetime.now(UTC),
    )

    invoice = relationship("Invoice", back_populates="pdf_exports")
    requested_by = relationship("Subscriber")


class CreditNoteStatus(enum.Enum):
    draft = "draft"
    issued = "issued"
    partially_applied = "partially_applied"
    applied = "applied"
    void = "void"


class CreditNote(Base):
    __tablename__ = "credit_notes"
    __table_args__ = (
        # Backs the ERP AR incremental sync watermark + the un-watermarked
        # default list sort (see the Invoice indexes for the incident context).
        Index(
            "ix_credit_notes_is_active_updated_at",
            "is_active",
            "updated_at",
        ),
        Index(
            "ix_credit_notes_is_active_created_at",
            "is_active",
            "created_at",
        ),
    )

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
    splynx_credit_note_id: Mapped[int | None] = mapped_column(Integer)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(UTC)
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(UTC),
        onupdate=lambda: datetime.now(UTC),
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
    metadata_: Mapped[dict | None] = mapped_column("metadata", JSONB)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(UTC)
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(UTC),
        onupdate=lambda: datetime.now(UTC),
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
        DateTime(timezone=True), default=lambda: datetime.now(UTC)
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(UTC),
        onupdate=lambda: datetime.now(UTC),
    )

    credit_note = relationship("CreditNote", back_populates="applications")
    invoice = relationship("Invoice", back_populates="credit_note_applications")


class InvoiceLine(Base):
    __tablename__ = "invoice_lines"
    __table_args__ = (
        Index("ix_invoice_lines_invoice_id", "invoice_id"),
        Index(
            "uq_invoice_lines_active_billing_line_key",
            "billing_line_key",
            unique=True,
            postgresql_where=text("is_active AND billing_line_key IS NOT NULL"),
        ),
    )

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
    metadata_: Mapped[dict | None] = mapped_column("metadata", JSONB)
    billing_line_key: Mapped[str | None] = mapped_column(String(255))
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(UTC)
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(UTC),
        onupdate=lambda: datetime.now(UTC),
    )

    invoice = relationship("Invoice", back_populates="lines")
    tax_rate = relationship("TaxRate")


class PaymentMethod(Base):
    __tablename__ = "payment_methods"
    __table_args__ = (
        # Exactly one owner: a customer subscriber (account_id) OR — for a
        # first-class reseller_user login that has no backing subscriber
        # (Layer 3) — the reseller org (reseller_id). CASE-sum form works on
        # both Postgres and the SQLite test DB.
        CheckConstraint(
            "(CASE WHEN account_id IS NOT NULL THEN 1 ELSE 0 END"
            " + CASE WHEN reseller_id IS NOT NULL THEN 1 ELSE 0 END) = 1",
            name="ck_payment_methods_exactly_one_owner",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    account_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("subscribers.id"), nullable=True
    )
    reseller_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("resellers.id"), nullable=True
    )
    payment_channel_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("payment_channels.id")
    )
    method_type: Mapped[PaymentMethodType] = mapped_column(
        Enum(PaymentMethodType), default=PaymentMethodType.card
    )
    label: Mapped[str | None] = mapped_column(String(120))
    token: Mapped[str | None] = mapped_column(String(512))
    last4: Mapped[str | None] = mapped_column(String(4))
    brand: Mapped[str | None] = mapped_column(String(40))
    expires_month: Mapped[int | None] = mapped_column(Integer)
    expires_year: Mapped[int | None] = mapped_column(Integer)
    is_default: Mapped[bool] = mapped_column(Boolean, default=False)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(UTC)
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(UTC),
        onupdate=lambda: datetime.now(UTC),
    )

    account = relationship("Subscriber")
    reseller = relationship("Reseller")
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
    token: Mapped[str | None] = mapped_column(String(512))
    is_default: Mapped[bool] = mapped_column(Boolean, default=False)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(UTC)
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(UTC),
        onupdate=lambda: datetime.now(UTC),
    )

    account = relationship("Subscriber")
    payment_method = relationship("PaymentMethod")


class Payment(Base):
    __tablename__ = "payments"
    __table_args__ = (
        Index(
            "uq_payments_active_external_id",
            "provider_id",
            "external_id",
            unique=True,
            postgresql_where=text(
                "is_active AND provider_id IS NOT NULL AND external_id IS NOT NULL"
            ),
        ),
        Index(
            "uq_payments_active_splynx_payment_id",
            "splynx_payment_id",
            unique=True,
            postgresql_where=text("is_active AND splynx_payment_id IS NOT NULL"),
        ),
        # Idempotency backstop for CRM-originated payments. These are recorded
        # with external_id = "crm:<ref>" and NO provider_id, so they fall outside
        # uq_payments_active_external_id (which requires provider_id NOT NULL).
        # A concurrent /crm/payments push could otherwise double-record cash.
        Index(
            "uq_payments_active_crm_external_id",
            "external_id",
            unique=True,
            postgresql_where=text(
                "is_active AND external_id IS NOT NULL AND external_id LIKE 'crm:%'"
            ),
            # Keep the predicate partial on SQLite too (tests) so non-CRM
            # payments sharing an external_id aren't wrongly constrained.
            sqlite_where=text(
                "is_active AND external_id IS NOT NULL AND external_id LIKE 'crm:%'"
            ),
        ),
        # Backs the ERP AR incremental sync watermark + the un-watermarked
        # default list sort (see the Invoice indexes for the incident context).
        Index(
            "ix_payments_is_active_updated_at",
            "is_active",
            "updated_at",
        ),
        Index(
            "ix_payments_is_active_created_at",
            "is_active",
            "created_at",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    account_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("subscribers.id")
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
    billing_account_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("billing_accounts.id")
    )
    amount: Mapped[Decimal] = mapped_column(Numeric(12, 2), default=Decimal("0.00"))
    # Total refunded so far (sum of refund ledger entries), maintained by the
    # refund flow. `amount` stays the gross captured figure; net cash =
    # amount - refunded_amount. Exposed so ERP posts the net after a refund.
    refunded_amount: Mapped[Decimal] = mapped_column(
        Numeric(12, 2), default=Decimal("0.00"), server_default="0"
    )
    # Payment-gateway fee withheld from settlement (Paystack `fees`,
    # Flutterwave `app_fee`). `amount` stays the gross the customer was charged;
    # the gateway settles `amount - provider_fee` to the bank. Exposed so ERP can
    # split the receipt (Dr Bank net / Dr charges / Cr AR gross) and bank rec ties.
    provider_fee: Mapped[Decimal] = mapped_column(
        Numeric(12, 2), default=Decimal("0.00"), server_default="0"
    )
    currency: Mapped[str] = mapped_column(String(3), default="NGN")
    status: Mapped[PaymentStatus] = mapped_column(
        Enum(PaymentStatus), default=PaymentStatus.pending
    )
    paid_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    external_id: Mapped[str | None] = mapped_column(String(120))
    memo: Mapped[str | None] = mapped_column(Text)
    receipt_number: Mapped[str | None] = mapped_column(String(120))
    splynx_payment_id: Mapped[int | None] = mapped_column(Integer)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(UTC)
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(UTC),
        onupdate=lambda: datetime.now(UTC),
    )

    account = relationship("Subscriber")
    payment_method = relationship("PaymentMethod", back_populates="payments")
    payment_channel = relationship("PaymentChannel", back_populates="payments")
    collection_account = relationship("CollectionAccount", back_populates="payments")
    provider = relationship("PaymentProvider", back_populates="payments")
    billing_account = relationship("BillingAccount", back_populates="payments")
    provider_events = relationship("PaymentProviderEvent", back_populates="payment")
    ledger_entries = relationship("LedgerEntry", back_populates="payment")
    dunning_actions = relationship("DunningActionLog", back_populates="payment")
    allocations = relationship("PaymentAllocation", back_populates="payment")
    topup_intents = relationship("TopupIntent", back_populates="completed_payment")


class TopupIntent(Base):
    __tablename__ = "topup_intents"
    __table_args__ = (
        Index("ix_topup_intents_account_id", "account_id"),
        Index("uq_topup_intents_reference", "reference", unique=True),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    account_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("subscribers.id")
    )
    billing_account_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("billing_accounts.id")
    )
    completed_payment_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("payments.id")
    )
    reference: Mapped[str] = mapped_column(String(120), nullable=False)
    provider_type: Mapped[str] = mapped_column(String(40), nullable=False)
    currency: Mapped[str] = mapped_column(String(3), default="NGN")
    requested_amount: Mapped[Decimal] = mapped_column(
        Numeric(12, 2), default=Decimal("0.00")
    )
    actual_amount: Mapped[Decimal | None] = mapped_column(Numeric(12, 2))
    external_id: Mapped[str | None] = mapped_column(String(120))
    status: Mapped[str] = mapped_column(String(20), default="pending")
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    metadata_: Mapped[dict | None] = mapped_column("metadata", JSONB)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(UTC)
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(UTC),
        onupdate=lambda: datetime.now(UTC),
    )

    account = relationship("Subscriber")
    billing_account = relationship("BillingAccount")
    completed_payment = relationship("Payment", back_populates="topup_intents")


class PaymentAllocation(Base):
    __tablename__ = "payment_allocations"
    __table_args__ = (
        UniqueConstraint(
            "payment_id", "invoice_id", name="uq_payment_allocations_payment_invoice"
        ),
        # The unique constraint's index is payment_id-leading; this backs
        # invoice_id-leading lookups (allocations for an invoice).
        Index("ix_payment_allocations_invoice_id", "invoice_id"),
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
    is_active: Mapped[bool] = mapped_column(
        Boolean, default=True, server_default="true"
    )

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(UTC)
    )

    payment = relationship("Payment", back_populates="allocations")
    invoice = relationship("Invoice", back_populates="payment_allocations")


class LedgerEntry(Base):
    __tablename__ = "ledger_entries"
    __table_args__ = (
        # One reversal per entry, enforced by the database rather than by caller
        # discipline. NULLs are distinct in a unique index on both Postgres and
        # SQLite, so ordinary (non-reversal) entries are unconstrained while every
        # non-null reversal_of_entry_id is unique.
        #
        # Deliberately NOT scoped to is_active: a reversal that was later
        # deactivated must still block a second reversal of the same entry, or
        # deactivating the reversal would silently re-open the double-post.
        Index(
            "uq_ledger_entries_reversal_of",
            "reversal_of_entry_id",
            unique=True,
        ),
    )

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
    category: Mapped[LedgerCategory | None] = mapped_column(
        Enum(LedgerCategory, name="ledgercategory", create_constraint=False),
        nullable=True,
    )
    amount: Mapped[Decimal] = mapped_column(Numeric(12, 2), default=Decimal("0.00"))
    currency: Mapped[str] = mapped_column(String(3), default="NGN")
    memo: Mapped[str | None] = mapped_column(Text)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)

    # The entry this one reverses. The ledger is append-only: an entry's effect is
    # undone by posting its opposite, and this is the structural link between the
    # two. ``uq_ledger_entries_reversal_of`` makes a second reversal of the same
    # entry impossible at the database level, so the invariant does not depend on
    # every future caller remembering to take the row lock first.
    #
    # NULL for ordinary entries and for pre-existing reversals, which were only
    # ever linked by memo text. Those are deliberately NOT backfilled here — the
    # pairing is inferred, and inferring it wrong would corrupt money. The service
    # keeps the legacy memo lookup so an un-backfilled reversal still blocks a
    # re-reversal.
    reversal_of_entry_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("ledger_entries.id", ondelete="RESTRICT"),
        nullable=True,
    )

    # The real-world date the entry represents (invoice issue / payment / txn
    # date). NULL for native entries and any row the cutover backfill could not
    # resolve; display/order should COALESCE(effective_date, created_at). The
    # migrated ledger lost original dates — created_at is the import instant.
    effective_date: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(UTC)
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(UTC),
        onupdate=lambda: datetime.now(UTC),
    )

    account = relationship("Subscriber")
    invoice = relationship("Invoice", back_populates="ledger_entries")
    payment = relationship("Payment", back_populates="ledger_entries")


class ServiceEntitlement(Base):
    """Funded prepaid service period.

    Money remains represented by invoices, payments, allocations, and ledger
    entries. This row is the prepaid access proof created only after a service
    period has been funded.
    """

    __tablename__ = "service_entitlements"
    __table_args__ = (
        Index(
            "ix_service_entitlements_account_subscription_period",
            "account_id",
            "subscription_id",
            "starts_at",
            "ends_at",
        ),
        Index(
            "uq_service_entitlements_active_invoice_line",
            "source_invoice_line_id",
            unique=True,
            postgresql_where=text(
                "status = 'active' AND source_invoice_line_id IS NOT NULL"
            ),
            sqlite_where=text(
                "status = 'active' AND source_invoice_line_id IS NOT NULL"
            ),
        ),
        Index(
            "uq_service_entitlements_active_ledger_entry",
            "source_ledger_entry_id",
            unique=True,
            postgresql_where=text(
                "status = 'active' AND source_ledger_entry_id IS NOT NULL"
            ),
            sqlite_where=text(
                "status = 'active' AND source_ledger_entry_id IS NOT NULL"
            ),
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    account_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("subscribers.id"), nullable=False
    )
    subscription_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("subscriptions.id"), nullable=False
    )
    source_invoice_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("invoices.id")
    )
    source_invoice_line_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("invoice_lines.id")
    )
    source_ledger_entry_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("ledger_entries.id")
    )
    starts_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    ends_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    amount_funded: Mapped[Decimal] = mapped_column(
        Numeric(12, 2), default=Decimal("0.00")
    )
    currency: Mapped[str] = mapped_column(String(3), default="NGN")
    status: Mapped[ServiceEntitlementStatus] = mapped_column(
        Enum(ServiceEntitlementStatus), default=ServiceEntitlementStatus.active
    )
    metadata_: Mapped[dict | None] = mapped_column("metadata", JSONB)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(UTC)
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(UTC),
        onupdate=lambda: datetime.now(UTC),
    )

    account = relationship("Subscriber")
    subscription = relationship("Subscription")
    source_invoice = relationship("Invoice")
    source_invoice_line = relationship("InvoiceLine")
    source_ledger_entry = relationship("LedgerEntry")


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
        DateTime(timezone=True), default=lambda: datetime.now(UTC)
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(UTC),
        onupdate=lambda: datetime.now(UTC),
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
        DateTime(timezone=True), default=lambda: datetime.now(UTC)
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(UTC),
        onupdate=lambda: datetime.now(UTC),
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
        DateTime(timezone=True), default=lambda: datetime.now(UTC)
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(UTC),
        onupdate=lambda: datetime.now(UTC),
    )

    payments = relationship("Payment", back_populates="collection_account")
    channel_mappings = relationship(
        "PaymentChannelAccount", back_populates="collection_account"
    )


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
        DateTime(timezone=True), default=lambda: datetime.now(UTC)
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(UTC),
        onupdate=lambda: datetime.now(UTC),
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
        DateTime(timezone=True), default=lambda: datetime.now(UTC)
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(UTC),
        onupdate=lambda: datetime.now(UTC),
    )

    channel = relationship("PaymentChannel", back_populates="channel_accounts")
    collection_account = relationship(
        "CollectionAccount", back_populates="channel_mappings"
    )


class PaymentProviderEvent(Base):
    __tablename__ = "payment_provider_events"
    __table_args__ = (
        UniqueConstraint(
            "provider_id",
            "idempotency_key",
            name="uq_payment_provider_events_idempotency",
        ),
        Index(
            "uq_payment_provider_events_external_id",
            "provider_id",
            "external_id",
            unique=True,
            postgresql_where=text("external_id IS NOT NULL"),
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
        DateTime(timezone=True), default=lambda: datetime.now(UTC)
    )
    processed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    provider = relationship("PaymentProvider", back_populates="events")
    payment = relationship("Payment", back_populates="provider_events")
    invoice = relationship("Invoice")


class PaymentWebhookDeadLetter(Base):
    """Durable capture of an inbound payment-provider webhook.

    A row is written (and committed in its own transaction) the moment a
    signature-verified webhook arrives, *before* ingest is attempted, so the
    raw payload survives even if ingest's transaction rolls back or the worker
    dies mid-processing. On success the row is deleted; on failure it is kept
    (status ``failed``/``rejected``) for replay. This closes the silent-loss
    gap where a transient ingest error returned HTTP 200 and the provider never
    retried.

    ``provider_type`` is stored as a plain string (not an FK) because a webhook
    can arrive before — or without — a matching provider being configured, and
    we must never lose it for that reason.
    """

    __tablename__ = "payment_webhook_dead_letters"
    __table_args__ = (
        Index(
            "ix_payment_webhook_dead_letters_status",
            "status",
        ),
        Index(
            "ix_payment_webhook_dead_letters_provider_idem",
            "provider_type",
            "idempotency_key",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    provider_type: Mapped[str] = mapped_column(String(40), nullable=False)
    event_type: Mapped[str | None] = mapped_column(String(120))
    external_id: Mapped[str | None] = mapped_column(String(160))
    idempotency_key: Mapped[str | None] = mapped_column(String(160))
    status: Mapped[PaymentWebhookDeadLetterStatus] = mapped_column(
        Enum(PaymentWebhookDeadLetterStatus),
        default=PaymentWebhookDeadLetterStatus.received,
        nullable=False,
    )
    payload: Mapped[dict | None] = mapped_column(JSON)
    error: Mapped[str | None] = mapped_column(Text)
    retry_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    received_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(UTC)
    )
    last_attempt_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


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
        DateTime(timezone=True), default=lambda: datetime.now(UTC)
    )
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    subscriptions_scanned: Mapped[int] = mapped_column(Integer, default=0)
    subscriptions_billed: Mapped[int] = mapped_column(Integer, default=0)
    invoices_created: Mapped[int] = mapped_column(Integer, default=0)
    lines_created: Mapped[int] = mapped_column(Integer, default=0)
    skipped: Mapped[int] = mapped_column(Integer, default=0)
    error: Mapped[str | None] = mapped_column(Text)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(UTC)
    )


class BillingRunSchedule(Base):
    __tablename__ = "billing_run_schedules"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    enabled: Mapped[bool] = mapped_column(Boolean, default=False)
    run_day: Mapped[int] = mapped_column(Integer, default=1)
    run_time: Mapped[str] = mapped_column(String(8), default="02:00")
    timezone: Mapped[str] = mapped_column(String(64), default="Africa/Lagos")
    billing_cycle: Mapped[str] = mapped_column(String(40), default="monthly")
    partner_ids: Mapped[list | None] = mapped_column(JSON, default=list)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(UTC)
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(UTC),
        onupdate=lambda: datetime.now(UTC),
    )


class BankReconciliationRun(Base):
    __tablename__ = "bank_reconciliation_runs"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    date_range: Mapped[str | None] = mapped_column(String(20))
    handler: Mapped[str | None] = mapped_column(String(120))
    statement_rows: Mapped[int] = mapped_column(Integer, default=0)
    imported_rows: Mapped[int] = mapped_column(Integer, default=0)
    unmatched_rows: Mapped[int] = mapped_column(Integer, default=0)
    system_payment_count: Mapped[int] = mapped_column(Integer, default=0)
    statement_total: Mapped[Decimal] = mapped_column(
        Numeric(14, 2), default=Decimal("0.00")
    )
    payment_total: Mapped[Decimal] = mapped_column(
        Numeric(14, 2), default=Decimal("0.00")
    )
    difference_total: Mapped[Decimal] = mapped_column(
        Numeric(14, 2), default=Decimal("0.00")
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(UTC)
    )

    items = relationship("BankReconciliationItem", back_populates="run")


class BankReconciliationItem(Base):
    __tablename__ = "bank_reconciliation_items"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    run_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("bank_reconciliation_runs.id"), nullable=False
    )
    item_type: Mapped[str] = mapped_column(String(20), default="unmatched")
    reference: Mapped[str | None] = mapped_column(String(255))
    file_name: Mapped[str | None] = mapped_column(String(255))
    count: Mapped[int] = mapped_column(Integer, default=0)
    amount: Mapped[Decimal] = mapped_column(Numeric(14, 2), default=Decimal("0.00"))
    metadata_: Mapped[dict | None] = mapped_column("metadata", JSON)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(UTC)
    )

    run = relationship("BankReconciliationRun", back_populates="items")
