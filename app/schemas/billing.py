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
    is_active: bool | None = None

    @model_validator(mode="after")
    def _validate_status_timestamps(self) -> "InvoiceUpdate":
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

    id: UUID
    created_at: datetime
    updated_at: datetime


class CreditNoteBase(BaseModel):
    account_id: UUID
    invoice_id: UUID | None = None
    credit_number: str | None = Field(default=None, max_length=80)
    status: CreditNoteStatus = CreditNoteStatus.draft
    currency: str = Field(default="NGN", min_length=3, max_length=3)
    subtotal: Decimal = Field(default=Decimal("0.00"), ge=0)
    tax_total: Decimal = Field(default=Decimal("0.00"), ge=0)
    total: Decimal = Field(default=Decimal("0.00"), ge=0)
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
    subtotal: Decimal | None = Field(default=None, ge=0)
    tax_total: Decimal | None = Field(default=None, ge=0)
    total: Decimal | None = Field(default=None, ge=0)
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


class CreditNoteApplyRequest(BaseModel):
    invoice_id: UUID
    amount: Decimal | None = Field(default=None, gt=0)
    memo: str | None = None


class PaymentMethodBase(BaseModel):
    account_id: UUID
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
    account_id: UUID
    invoice_id: UUID | None = None
    payment_method_id: UUID | None = None
    payment_channel_id: UUID | None = None
    collection_account_id: UUID | None = None
    provider_id: UUID | None = None
    amount: Decimal = Field(gt=0)
    currency: str = Field(default="NGN", min_length=3, max_length=3)
    status: PaymentStatus = PaymentStatus.pending
    paid_at: datetime | None = None
    external_id: str | None = Field(default=None, max_length=120)
    memo: str | None = None
    is_active: bool = True


class PaymentCreate(PaymentBase):
    allocations: list["PaymentAllocationApply"] | None = None


class PaymentUpdate(BaseModel):
    account_id: UUID | None = None
    invoice_id: UUID | None = None
    payment_method_id: UUID | None = None
    payment_channel_id: UUID | None = None
    collection_account_id: UUID | None = None
    provider_id: UUID | None = None
    amount: Decimal | None = Field(default=None, gt=0)
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
    allocations: list["PaymentAllocationRead"] = Field(default_factory=list)


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
    event_type: str = Field(min_length=1, max_length=120)
    external_id: str | None = Field(default=None, max_length=160)
    idempotency_key: str | None = Field(default=None, max_length=160)
    amount: Decimal | None = Field(default=None, gt=0)
    currency: str | None = Field(default=None, min_length=3, max_length=3)
    payload: dict | None = None


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
    created_at: datetime


class TaxRateBase(BaseModel):
    name: str = Field(min_length=1, max_length=120)
    code: str | None = Field(default=None, max_length=40)
    rate: Decimal = Field(default=Decimal("0.0000"), ge=0)
    is_active: bool = True
    description: str | None = None


class TaxRateCreate(TaxRateBase):
    pass


class TaxRateUpdate(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=120)
    code: str | None = Field(default=None, max_length=40)
    rate: Decimal | None = Field(default=None, ge=0)
    is_active: bool | None = None
    description: str | None = None


class TaxRateRead(TaxRateBase):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    created_at: datetime
    updated_at: datetime


class InvoiceRead(InvoiceBase):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    created_at: datetime
    updated_at: datetime
    lines: list[InvoiceLineRead] = Field(default_factory=list)
    payments: list[PaymentRead] = Field(default_factory=list)


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
