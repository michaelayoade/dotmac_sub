from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, model_validator

from app.models.billing import (
    BankAccountType,
    BillingRunStatus,
    CollectionAccountType,
    CreditNoteStatus,
    InvoiceStatus,
    LedgerEntryType,
    LedgerSource,
    PaymentChannelType,
    PaymentMethodType,
    PaymentProviderEventStatus,
    PaymentProviderType,
    PaymentStatus,
    PaymentWebhookDeadLetterStatus,
    TaxApplication,
)
from app.models.catalog import BillingCycle


class InvoiceBase(BaseModel):
    account_id: UUID
    invoice_number: str | None = Field(default=None, max_length=80)
    status: InvoiceStatus = InvoiceStatus.draft
    currency: str = Field(default="NGN", min_length=3, max_length=3)
    subtotal: Decimal = Field(default=Decimal("0.00"), ge=0)
    tax_total: Decimal = Field(default=Decimal("0.00"), ge=0)
    total: Decimal = Field(default=Decimal("0.00"), ge=0)
    balance_due: Decimal = Field(default=Decimal("0.00"), ge=0)
    billing_period_start: datetime | None = None
    billing_period_end: datetime | None = None
    issued_at: datetime | None = None
    due_at: datetime | None = None
    paid_at: datetime | None = None
    memo: str | None = None
    is_proforma: bool = False
    is_active: bool = True


class InvoiceCreate(InvoiceBase):
    pass


class InvoiceUpdate(BaseModel):
    account_id: UUID | None = None
    invoice_number: str | None = Field(default=None, max_length=80)
    status: InvoiceStatus | None = None
    currency: str | None = Field(default=None, min_length=3, max_length=3)
    subtotal: Decimal | None = Field(default=None, ge=0)
    tax_total: Decimal | None = Field(default=None, ge=0)
    total: Decimal | None = Field(default=None, ge=0)
    balance_due: Decimal | None = Field(default=None, ge=0)
    issued_at: datetime | None = None
    due_at: datetime | None = None
    paid_at: datetime | None = None
    memo: str | None = None
    is_proforma: bool | None = None
    is_active: bool | None = None

    @model_validator(mode="after")
    def _validate_status_timestamps(self) -> InvoiceUpdate:
        fields_set = self.model_fields_set
        if "status" in fields_set and self.status == InvoiceStatus.paid:
            if "paid_at" not in fields_set or self.paid_at is None:
                raise ValueError("paid_at is required when status is paid")
        return self


class InvoiceLineBase(BaseModel):
    model_config = ConfigDict(populate_by_name=True)
    invoice_id: UUID
    subscription_id: UUID | None = None
    description: str = Field(min_length=1, max_length=255)
    quantity: Decimal = Field(default=Decimal("1.000"), gt=0)
    unit_price: Decimal = Field(default=Decimal("0.00"), ge=0)
    amount: Decimal | None = Field(default=None, ge=0)
    tax_rate_id: UUID | None = None
    tax_application: TaxApplication = TaxApplication.exclusive
    metadata_: str | None = Field(
        default=None,
        serialization_alias="metadata",
    )
    is_active: bool = True


class InvoiceLineCreate(InvoiceLineBase):
    pass


class InvoiceLineUpdate(BaseModel):
    model_config = ConfigDict(populate_by_name=True)
    invoice_id: UUID | None = None
    description: str | None = Field(default=None, min_length=1, max_length=255)
    quantity: Decimal | None = Field(default=None, gt=0)
    unit_price: Decimal | None = Field(default=None, ge=0)
    amount: Decimal | None = Field(default=None, ge=0)
    tax_rate_id: UUID | None = None
    tax_application: TaxApplication | None = None
    metadata_: str | None = Field(
        default=None,
        serialization_alias="metadata",
    )
    is_active: bool | None = None


class InvoiceLineRead(InvoiceLineBase):
    model_config = ConfigDict(from_attributes=True, populate_by_name=True)

    # Read model reflects what's stored — credit/adjustment lines are
    # legitimately negative, so don't inherit the create-side ge=0 constraint
    # (it 500s response serialization for any invoice with a negative line).
    unit_price: Decimal = Decimal("0.00")
    amount: Decimal | None = None
    id: UUID
    created_at: datetime
    updated_at: datetime


class CreditNoteBase(BaseModel):
    account_id: UUID
    invoice_id: UUID | None = None
    credit_number: str | None = Field(default=None, max_length=80)
    status: CreditNoteStatus = CreditNoteStatus.draft
    currency: str = Field(default="NGN", min_length=3, max_length=3)
    subtotal: Decimal = Field(default=Decimal("0.00"), ge=0, lt=10000000000)
    tax_total: Decimal = Field(default=Decimal("0.00"), ge=0, lt=10000000000)
    total: Decimal = Field(default=Decimal("0.00"), ge=0, lt=10000000000)
    memo: str | None = None
    is_active: bool = True


class CreditNoteCreate(CreditNoteBase):
    pass


class CreditNoteUpdate(BaseModel):
    account_id: UUID | None = None
    invoice_id: UUID | None = None
    credit_number: str | None = Field(default=None, max_length=80)
    status: CreditNoteStatus | None = None
    currency: str | None = Field(default=None, min_length=3, max_length=3)
    subtotal: Decimal | None = Field(default=None, ge=0, lt=10000000000)
    tax_total: Decimal | None = Field(default=None, ge=0, lt=10000000000)
    total: Decimal | None = Field(default=None, ge=0, lt=10000000000)
    memo: str | None = None
    is_active: bool | None = None


class CreditNoteLineBase(BaseModel):
    model_config = ConfigDict(populate_by_name=True)
    credit_note_id: UUID
    description: str = Field(min_length=1, max_length=255)
    quantity: Decimal = Field(default=Decimal("1.000"), gt=0)
    unit_price: Decimal = Field(default=Decimal("0.00"), ge=0)
    amount: Decimal | None = Field(default=None, ge=0)
    tax_rate_id: UUID | None = None
    tax_application: TaxApplication = TaxApplication.exclusive
    metadata_: str | None = Field(
        default=None,
        serialization_alias="metadata",
    )
    is_active: bool = True


class CreditNoteLineCreate(CreditNoteLineBase):
    pass


class CreditNoteLineUpdate(BaseModel):
    credit_note_id: UUID | None = None
    description: str | None = Field(default=None, min_length=1, max_length=255)
    quantity: Decimal | None = Field(default=None, gt=0)
    unit_price: Decimal | None = Field(default=None, ge=0)
    amount: Decimal | None = Field(default=None, ge=0)
    tax_rate_id: UUID | None = None
    tax_application: TaxApplication | None = None
    metadata_: str | None = Field(
        default=None,
        serialization_alias="metadata",
    )
    is_active: bool | None = None


class CreditNoteLineRead(CreditNoteLineBase):
    model_config = ConfigDict(from_attributes=True, populate_by_name=True)

    id: UUID
    created_at: datetime
    updated_at: datetime


class CreditNoteApplicationBase(BaseModel):
    credit_note_id: UUID
    invoice_id: UUID
    amount: Decimal = Field(default=Decimal("0.00"), ge=0)
    memo: str | None = None


class CreditNoteApplicationRead(CreditNoteApplicationBase):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    created_at: datetime
    updated_at: datetime


class CreditNoteApplyRequest(BaseModel):
    invoice_id: UUID
    amount: Decimal | None = Field(default=None, gt=0)
    memo: str | None = None


class PaymentMethodBase(BaseModel):
    # Exactly one owner: customer subscriber (account_id) or reseller org
    # (reseller_id, for subscriber-less reseller_user logins — Layer 3).
    account_id: UUID | None = None
    reseller_id: UUID | None = None
    payment_channel_id: UUID | None = None
    method_type: PaymentMethodType = PaymentMethodType.card
    label: str | None = Field(default=None, max_length=120)
    token: str | None = Field(default=None, max_length=255)
    last4: str | None = Field(default=None, max_length=4)
    brand: str | None = Field(default=None, max_length=40)
    expires_month: int | None = Field(default=None, ge=1, le=12)
    expires_year: int | None = Field(default=None, ge=2000, le=2100)
    is_default: bool = False
    is_active: bool = True


class PaymentMethodCreate(PaymentMethodBase):
    pass


class PaymentMethodUpdate(BaseModel):
    account_id: UUID | None = None
    payment_channel_id: UUID | None = None
    method_type: PaymentMethodType | None = None
    label: str | None = Field(default=None, max_length=120)
    token: str | None = Field(default=None, max_length=255)
    last4: str | None = Field(default=None, max_length=4)
    brand: str | None = Field(default=None, max_length=40)
    expires_month: int | None = Field(default=None, ge=1, le=12)
    expires_year: int | None = Field(default=None, ge=2000, le=2100)
    is_default: bool | None = None
    is_active: bool | None = None


class PaymentMethodRead(PaymentMethodBase):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    created_at: datetime
    updated_at: datetime


class BankAccountBase(BaseModel):
    account_id: UUID
    payment_method_id: UUID | None = None
    bank_name: str | None = Field(default=None, max_length=120)
    account_type: BankAccountType = BankAccountType.checking
    account_last4: str | None = Field(default=None, max_length=4)
    routing_last4: str | None = Field(default=None, max_length=4)
    token: str | None = Field(default=None, max_length=255)
    is_default: bool = False
    is_active: bool = True


class BankAccountCreate(BankAccountBase):
    pass


class BankAccountUpdate(BaseModel):
    account_id: UUID | None = None
    payment_method_id: UUID | None = None
    bank_name: str | None = Field(default=None, max_length=120)
    account_type: BankAccountType | None = None
    account_last4: str | None = Field(default=None, max_length=4)
    routing_last4: str | None = Field(default=None, max_length=4)
    token: str | None = Field(default=None, max_length=255)
    is_default: bool | None = None
    is_active: bool | None = None


class BankAccountRead(BankAccountBase):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    created_at: datetime
    updated_at: datetime


class PaymentBase(BaseModel):
    account_id: UUID | None = None
    billing_account_id: UUID | None = None
    payment_method_id: UUID | None = None
    payment_channel_id: UUID | None = None
    collection_account_id: UUID | None = None
    provider_id: UUID | None = None
    amount: Decimal = Field(gt=0, lt=10000000000)
    currency: str = Field(default="NGN", min_length=3, max_length=3)
    status: PaymentStatus = PaymentStatus.pending
    paid_at: datetime | None = None
    external_id: str | None = Field(default=None, max_length=120)
    memo: str | None = None
    is_active: bool = True

    @model_validator(mode="after")
    def _exactly_one_account_scope(self) -> PaymentBase:
        if (self.account_id is None) == (self.billing_account_id is None):
            raise ValueError(
                "exactly one of account_id or billing_account_id must be set"
            )
        return self


class PaymentCreate(PaymentBase):
    allocations: list[PaymentAllocationApply] | None = None


class PaymentUpdate(BaseModel):
    account_id: UUID | None = None
    billing_account_id: UUID | None = None
    payment_method_id: UUID | None = None
    payment_channel_id: UUID | None = None
    collection_account_id: UUID | None = None
    provider_id: UUID | None = None
    amount: Decimal | None = Field(default=None, gt=0, lt=10000000000)
    currency: str | None = Field(default=None, min_length=3, max_length=3)
    status: PaymentStatus | None = None
    paid_at: datetime | None = None
    external_id: str | None = Field(default=None, max_length=120)
    memo: str | None = None
    is_active: bool | None = None


class PaymentRead(PaymentBase):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    created_at: datetime
    updated_at: datetime
    allocations: list[PaymentAllocationRead] = Field(default_factory=list)


# --- Customer-initiated online payment (hosted checkout) ------------------
# These power the self-care mobile/SPA flow: the client initiates a provider
# checkout for one of *its own* invoices, completes payment with the provider
# SDK using the returned public key + reference, then asks the server to verify
# and record it. Scoping is by the authenticated principal, not a permission.


class PaymentInitiateRequest(BaseModel):
    invoice_id: UUID
    # Which online gateway to checkout with for a new card; ignored for saved
    # cards. Defaults to the configured provider when omitted.
    provider: str | None = None
    # Charge this saved card server-side (one-tap); idempotency_key makes that
    # charge safe against a double-submit.
    payment_method_id: UUID | None = None
    idempotency_key: str | None = None


class PaymentInitiateResponse(BaseModel):
    invoice_id: UUID
    invoice_number: str | None = None
    amount: Decimal
    currency: str = "NGN"
    provider_type: str
    provider_public_key: str | None = None
    payment_reference: str
    customer_email: str | None = None
    # True when a saved card was charged server-side — skip the gateway webview
    # and go straight to verify (mirrors the top-up flow).
    charged: bool = False
    checkout_url: str | None = None


class PaymentVerifyRequest(BaseModel):
    reference: str = Field(min_length=1)
    provider: str | None = None
    save_card: bool = False


class PaymentVerifyResponse(BaseModel):
    reference: str
    payment_id: UUID
    invoice_id: UUID | None = None
    amount: Decimal
    currency: str = "NGN"
    status: str
    already_recorded: bool = False


# --- Prepaid account top-up (hosted checkout) -----------------------------


class PaymentProviderOption(BaseModel):
    """An online checkout option (Paystack/Flutterwave) for the pay selector."""

    provider_type: str
    label: str


class BankTransferAccount(BaseModel):
    bank_name: str
    account_name: str
    account_number: str
    sort_code: str | None = None


class DirectBankTransferConfig(BaseModel):
    """Admin-configured bank account(s) for the direct-transfer pay option."""

    enabled: bool = False
    instructions: str | None = None
    accounts: list[BankTransferAccount] = Field(default_factory=list)


class TopupPageResponse(BaseModel):
    provider_type: str
    provider_public_key: str | None = None
    currency: str = "NGN"
    prepaid_balance: Decimal | None = None
    min_amount: int
    max_amount: int
    preset_amounts: list[int] = Field(default_factory=list)
    customer_email: str | None = None
    # The full pay-with selector: online gateways (Paystack/Flutterwave) and the
    # direct-bank-transfer option, mirroring the web top-up chooser. Mobile
    # renders one row per online option plus a bank-transfer + saved-card flow.
    payment_options: list[PaymentProviderOption] = Field(default_factory=list)
    direct_bank_transfer: DirectBankTransferConfig | None = None


class TopupInitiateRequest(BaseModel):
    amount: Decimal = Field(gt=0)
    # Which online gateway to checkout with when paying by a new card. Defaults
    # to the configured provider when omitted; ignored for saved-card charges.
    provider: str | None = None
    # When set, charge this saved card server-side (one-tap repeat pay) instead
    # of opening the gateway checkout. idempotency_key makes that charge safe
    # against a double-tap (a replay returns the original intent).
    payment_method_id: UUID | None = None
    idempotency_key: str | None = None


class TopupInitiateResponse(BaseModel):
    intent_id: str
    provider_type: str
    provider_public_key: str | None = None
    payment_reference: str
    amount: Decimal
    currency: str = "NGN"
    customer_email: str | None = None
    # True when a saved card was charged server-side — the client should skip
    # the gateway webview and go straight to verify.
    charged: bool = False
    checkout_url: str | None = None


class TopupVerifyRequest(BaseModel):
    reference: str = Field(min_length=1)
    save_card: bool = False


class TopupVerifyResponse(BaseModel):
    reference: str
    amount: Decimal
    currency: str = "NGN"
    already_recorded: bool = False
    available_balance: Decimal | None = None
    credit_added: Decimal | None = None


class PaymentAllocationBase(BaseModel):
    payment_id: UUID
    invoice_id: UUID
    amount: Decimal = Field(default=Decimal("0.00"), ge=0)
    memo: str | None = None


class PaymentAllocationApply(BaseModel):
    invoice_id: UUID
    amount: Decimal = Field(default=Decimal("0.00"), gt=0)
    memo: str | None = None


class PaymentAllocationCreate(PaymentAllocationBase):
    pass


class PaymentAllocationRead(PaymentAllocationBase):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    created_at: datetime


class PaymentProviderBase(BaseModel):
    name: str = Field(min_length=1, max_length=160)
    provider_type: PaymentProviderType = PaymentProviderType.custom
    connector_config_id: UUID | None = None
    webhook_secret_ref: str | None = Field(default=None, max_length=255)
    is_active: bool = True
    notes: str | None = None


class PaymentProviderCreate(PaymentProviderBase):
    pass


class PaymentProviderUpdate(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=160)
    provider_type: PaymentProviderType | None = None
    connector_config_id: UUID | None = None
    webhook_secret_ref: str | None = Field(default=None, max_length=255)
    is_active: bool | None = None
    notes: str | None = None


class PaymentProviderRead(PaymentProviderBase):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    created_at: datetime
    updated_at: datetime


class PaymentProviderEventBase(BaseModel):
    provider_id: UUID
    payment_id: UUID | None = None
    invoice_id: UUID | None = None
    event_type: str = Field(min_length=1, max_length=120)
    external_id: str | None = Field(default=None, max_length=160)
    idempotency_key: str | None = Field(default=None, max_length=160)
    status: PaymentProviderEventStatus = PaymentProviderEventStatus.pending
    payload: dict | None = None
    error: str | None = None
    received_at: datetime | None = None
    processed_at: datetime | None = None


class PaymentProviderEventCreate(PaymentProviderEventBase):
    pass


class PaymentProviderEventRead(PaymentProviderEventBase):
    model_config = ConfigDict(from_attributes=True)

    id: UUID


class PaymentProviderEventIngest(BaseModel):
    provider_id: UUID
    payment_id: UUID | None = None
    invoice_id: UUID | None = None
    account_id: UUID | None = None
    # Reseller-consolidated billing account. Carried from the originating
    # TopupIntent so a consolidated webhook payment posts against the billing
    # account (crediting its balance / settling member invoices) instead of
    # landing with billing_account_id NULL and never settling. (cutover fix)
    billing_account_id: UUID | None = None
    event_type: str = Field(min_length=1, max_length=120)
    external_id: str | None = Field(default=None, max_length=160)
    idempotency_key: str | None = Field(default=None, max_length=160)
    amount: Decimal | None = Field(default=None, gt=0)
    currency: str | None = Field(default=None, min_length=3, max_length=3)
    payload: dict | None = None
    # Provider-aware status resolved by the webhook layer. Some providers reuse
    # one event type for both outcomes (Flutterwave's "charge.completed" carries
    # either a successful or a failed charge), so the raw event_type alone is
    # not always mappable. When set, this overrides the static event-type map.
    status_hint: PaymentStatus | None = None


class PaymentWebhookDeadLetterRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    provider_type: str
    event_type: str | None = None
    external_id: str | None = None
    idempotency_key: str | None = None
    status: PaymentWebhookDeadLetterStatus
    payload: dict | None = None
    error: str | None = None
    retry_count: int
    received_at: datetime
    last_attempt_at: datetime | None = None


class LedgerEntryBase(BaseModel):
    account_id: UUID
    invoice_id: UUID | None = None
    payment_id: UUID | None = None
    entry_type: LedgerEntryType
    source: LedgerSource | None = None
    amount: Decimal = Field(default=Decimal("0.00"), ge=0)
    currency: str = Field(default="NGN", min_length=3, max_length=3)
    memo: str | None = None
    is_active: bool = True


class LedgerEntryCreate(LedgerEntryBase):
    pass


class LedgerEntryUpdate(BaseModel):
    account_id: UUID | None = None
    invoice_id: UUID | None = None
    payment_id: UUID | None = None
    entry_type: LedgerEntryType | None = None
    source: LedgerSource | None = None
    amount: Decimal | None = Field(default=None, ge=0)
    currency: str | None = Field(default=None, min_length=3, max_length=3)
    memo: str | None = None
    is_active: bool | None = None


class LedgerEntryRead(LedgerEntryBase):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    # Real-world date of the entry (invoice issue / payment / imported txn date).
    # NULL for native and unbackfilled rows; clients should prefer it over
    # created_at (the import instant) and fall back to created_at when NULL.
    effective_date: datetime | None = None
    created_at: datetime
    updated_at: datetime


class TaxRateBase(BaseModel):
    name: str = Field(min_length=1, max_length=120)
    code: str | None = Field(default=None, max_length=40)
    rate: Decimal = Field(default=Decimal("0.0000"), ge=0, lt=100)
    is_active: bool = True
    description: str | None = None


class TaxRateCreate(TaxRateBase):
    pass


class TaxRateUpdate(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=120)
    code: str | None = Field(default=None, max_length=40)
    rate: Decimal | None = Field(default=None, ge=0, lt=100)
    is_active: bool | None = None
    description: str | None = None


class TaxRateRead(TaxRateBase):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    created_at: datetime
    updated_at: datetime


class InvoiceRead(InvoiceBase):
    model_config = ConfigDict(from_attributes=True)

    # Read model reflects stored data: a credit-heavy invoice can carry a
    # negative subtotal/total/balance, so don't inherit the create-side ge=0.
    subtotal: Decimal = Decimal("0.00")
    tax_total: Decimal = Decimal("0.00")
    total: Decimal = Decimal("0.00")
    balance_due: Decimal = Decimal("0.00")
    id: UUID
    created_at: datetime
    updated_at: datetime
    lines: list[InvoiceLineRead] = Field(default_factory=list)
    payment_allocations: list[PaymentAllocationRead] = Field(default_factory=list)


class CreditNoteRead(CreditNoteBase):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    applied_total: Decimal
    created_at: datetime
    updated_at: datetime
    lines: list[CreditNoteLineRead] = Field(default_factory=list)
    applications: list[CreditNoteApplicationRead] = Field(default_factory=list)


class InvoiceWriteOffRequest(BaseModel):
    memo: str | None = None


class InvoiceBulkWriteOffRequest(BaseModel):
    invoice_ids: list[UUID]
    memo: str | None = None


class InvoiceBulkVoidRequest(BaseModel):
    invoice_ids: list[UUID]
    memo: str | None = None


class InvoiceBulkActionResponse(BaseModel):
    updated: int


class InvoiceRunRequest(BaseModel):
    run_at: datetime | None = None
    billing_cycle: BillingCycle | None = None
    dry_run: bool = False


class InvoiceRunResponse(BaseModel):
    run_id: UUID | None = None
    run_at: datetime
    subscriptions_scanned: int
    subscriptions_billed: int
    invoices_created: int
    lines_created: int
    skipped: int


class BillingRunRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    run_at: datetime
    billing_cycle: str | None
    status: BillingRunStatus
    started_at: datetime
    finished_at: datetime | None
    subscriptions_scanned: int
    subscriptions_billed: int
    invoices_created: int
    lines_created: int
    skipped: int
    error: str | None
    created_at: datetime


class CollectionAccountBase(BaseModel):
    name: str = Field(min_length=1, max_length=160)
    account_type: CollectionAccountType = CollectionAccountType.bank
    bank_name: str | None = Field(default=None, max_length=120)
    account_last4: str | None = Field(default=None, max_length=4)
    currency: str = Field(default="NGN", min_length=3, max_length=3)
    is_active: bool = True
    notes: str | None = None


class CollectionAccountCreate(CollectionAccountBase):
    pass


class CollectionAccountUpdate(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=160)
    account_type: CollectionAccountType | None = None
    bank_name: str | None = Field(default=None, max_length=120)
    account_last4: str | None = Field(default=None, max_length=4)
    currency: str | None = Field(default=None, min_length=3, max_length=3)
    is_active: bool | None = None
    notes: str | None = None


class CollectionAccountRead(CollectionAccountBase):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    created_at: datetime
    updated_at: datetime


class PaymentChannelBase(BaseModel):
    name: str = Field(min_length=1, max_length=160)
    channel_type: PaymentChannelType = PaymentChannelType.other
    provider_id: UUID | None = None
    default_collection_account_id: UUID | None = None
    fee_rules: dict | None = None
    is_active: bool = True
    is_default: bool = False
    notes: str | None = None


class PaymentChannelCreate(PaymentChannelBase):
    pass


class PaymentChannelUpdate(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=160)
    channel_type: PaymentChannelType | None = None
    provider_id: UUID | None = None
    default_collection_account_id: UUID | None = None
    fee_rules: dict | None = None
    is_active: bool | None = None
    is_default: bool | None = None
    notes: str | None = None


class PaymentChannelRead(PaymentChannelBase):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    created_at: datetime
    updated_at: datetime


class PaymentChannelAccountBase(BaseModel):
    channel_id: UUID
    collection_account_id: UUID
    currency: str | None = Field(default=None, min_length=3, max_length=3)
    priority: int = 0
    is_default: bool = False
    is_active: bool = True


class PaymentChannelAccountCreate(PaymentChannelAccountBase):
    pass


class PaymentChannelAccountUpdate(BaseModel):
    channel_id: UUID | None = None
    collection_account_id: UUID | None = None
    currency: str | None = Field(default=None, min_length=3, max_length=3)
    priority: int | None = None
    is_default: bool | None = None
    is_active: bool | None = None


class PaymentChannelAccountRead(PaymentChannelAccountBase):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    created_at: datetime
    updated_at: datetime


class BillingAccountBase(BaseModel):
    reseller_id: UUID
    name: str = Field(min_length=1, max_length=160)
    currency: str = Field(default="NGN", min_length=3, max_length=3)
    status: str = Field(default="active", max_length=20)
    is_active: bool = True


class BillingAccountCreate(BillingAccountBase):
    pass


class BillingAccountUpdate(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=160)
    currency: str | None = Field(default=None, min_length=3, max_length=3)
    status: str | None = Field(default=None, max_length=20)
    is_active: bool | None = None


class BillingAccountRead(BillingAccountBase):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    balance: Decimal
    created_at: datetime
    updated_at: datetime


class BillingAccountConsolidatedPaymentCreate(BaseModel):
    amount: Decimal = Field(gt=0)
    currency: str = Field(default="NGN", min_length=3, max_length=3)
    paid_at: datetime | None = None
    memo: str | None = None
    payment_method_id: UUID | None = None
    payment_channel_id: UUID | None = None
    collection_account_id: UUID | None = None
    provider_id: UUID | None = None
    external_id: str | None = Field(default=None, max_length=120)
    allocations: list[PaymentAllocationApply] | None = None


class BillingAccountStatementSubscriberLine(BaseModel):
    subscriber_id: UUID
    subscriber_name: str
    open_invoice_count: int
    open_balance: Decimal


class BillingAccountStatementPayment(BaseModel):
    payment_id: UUID
    amount: Decimal
    currency: str
    paid_at: datetime | None
    memo: str | None
    allocated_total: Decimal
    unallocated_amount: Decimal


class BillingAccountStatement(BaseModel):
    billing_account: BillingAccountRead
    subscribers: list[BillingAccountStatementSubscriberLine]
    subscribers_total: int = 0
    recent_payments: list[BillingAccountStatementPayment]
    recent_payments_total: int = 0
    total_outstanding: Decimal
    unallocated_balance: Decimal


class AccountBalanceResponse(BaseModel):
    """Customer wallet/credit balance (positive = credit on file)."""

    credit_balance: Decimal = Decimal("0.00")
    currency: str = "NGN"


class MyPaymentMethodRead(BaseModel):
    """Customer-facing saved card — never exposes the reusable token."""

    model_config = ConfigDict(from_attributes=True)

    id: UUID
    method_type: PaymentMethodType
    label: str | None = None
    last4: str | None = None
    brand: str | None = None
    expires_month: int | None = None
    expires_year: int | None = None
    is_default: bool = False
    created_at: datetime


class AutopayStatusResponse(BaseModel):
    enabled: bool = False
    payment_method_id: UUID | None = None
    failure_count: int = 0
    last_failure_at: datetime | None = None
    last_failure_reason: str | None = None
    # Enabled but no longer charged (too many declines) until the customer
    # re-enables autopay or picks a new default card.
    suspended: bool = False


class AutopayEnableRequest(BaseModel):
    payment_method_id: UUID | None = None
