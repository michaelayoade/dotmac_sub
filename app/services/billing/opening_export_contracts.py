"""Read-only M8 source evidence; these values authorize no import or cutover."""

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from enum import StrEnum
from uuid import UUID

from app.models.billing import (
    InvoiceDueDateBasis,
    InvoiceStatus,
    PaymentProviderEventFinancialEffect,
    PaymentProviderEventSource,
    PaymentProviderEventStatus,
    PaymentRefundOrigin,
    PaymentReversalOrigin,
    PaymentSettlementOrigin,
    PaymentStatus,
    TaxApplication,
)
from app.models.billing_contract import BillingRecordAuthority
from app.models.payment_proof import PaymentProofStatus


class SourceKind(StrEnum):
    payment = "payment"
    credit_note = "credit_note"
    ledger = "ledger"
    posting_group = "posting_group"
    refund = "refund"
    reversal = "reversal"
    funding_baseline = "funding_baseline"
    opening_position = "opening_position"


class SettlementKind(StrEnum):
    payment = "payment"
    credit_note = "credit_note"
    opening_funding = "opening_funding"


class ExceptionCode(StrEnum):
    account_mapping = "account_mapping"
    source_after_cutoff = "source_after_cutoff"
    currency_precision = "currency_precision"
    settlement_mapping = "settlement_mapping"
    settlement_provenance = "settlement_provenance"
    residual_mismatch = "residual_mismatch"
    negative_residual = "negative_residual"
    excluded_nonzero_residue = "excluded_nonzero_residue"
    invoice_provenance = "invoice_provenance"
    duplicate_identity = "duplicate_identity"
    source_authority = "source_authority"
    unbacked_lane = "unbacked_lane"
    cash_unqualified = "cash_unqualified"
    cash_event_ambiguity = "cash_event_ambiguity"


class ExclusionReason(StrEnum):
    no_financial_activity = "no_financial_activity"
    draft = "draft"
    void = "void"
    fully_closed = "fully_closed"
    inactive_or_proforma = "inactive_or_proforma"


@dataclass(frozen=True)
class PreviewBillingOpeningExportQuery:
    # Caller declarations are retained verbatim; they are not authenticated proof.
    source_repository_sha: str
    installation_id: UUID
    economic_cutoff: datetime
    # Explicit source precision evidence, never inferred from an amount.
    currency_precisions: tuple[tuple[str, int], ...]
    population_version: str = "sub-billing-opening-census-v1"


@dataclass(frozen=True)
class CaptureEvidence:
    declared_source: PreviewBillingOpeningExportQuery
    schema_heads: tuple[str, ...]
    snapshot_id: str
    captured_at: datetime
    isolation: str
    population_complete: bool
    # Mutable documents are observed in this snapshot, not reconstructed as-of.
    mutable_history_reconstructed: bool = False


@dataclass(frozen=True)
class FinancialSourceEvidence:
    kind: SourceKind
    id: UUID
    account_id: UUID | None
    currency: str
    amount: Decimal | None
    secondary_amount: Decimal | None
    status: str
    active: bool
    invoice_id: UUID | None
    payment_id: UUID | None
    ledger_entry_id: UUID | None
    reverses_id: UUID | None
    created_at: datetime
    updated_at: datetime | None
    economic_at: datetime | None
    authority: BillingRecordAuthority | None = None
    source_kind: str | None = None
    source_id: UUID | None = None
    evidence_fingerprint: str | None = None
    consumption_ledger_entry_id: UUID | None = None


@dataclass(frozen=True)
class SettlementEvidence:
    kind: SettlementKind
    id: UUID
    invoice_id: UUID
    source_id: UUID
    amount: Decimal
    ledger_entry_id: UUID | None
    consumption_ledger_entry_id: UUID | None
    active: bool
    created_at: datetime
    recorded_account_id: UUID | None = None
    recorded_currency: str | None = None
    economic_at: datetime | None = None
    evidence_fingerprint: str | None = None


@dataclass(frozen=True)
class InvoiceLineEvidence:
    id: UUID
    invoice_id: UUID
    subscription_id: UUID | None
    amount: Decimal
    quantity: Decimal
    unit_price: Decimal
    active: bool
    billing_line_key: str | None
    created_at: datetime
    updated_at: datetime


@dataclass(frozen=True)
class InvoiceOpeningEvidence:
    invoice_id: UUID
    account_id: UUID
    currency: str
    precision: int | None
    invoice_number: str | None
    status: InvoiceStatus
    active: bool
    proforma: bool
    issued_at: datetime | None
    due_at: datetime | None
    due_basis: InvoiceDueDateBasis | None
    due_basis_ref: str | None
    due_policy_version: str | None
    total: Decimal
    stored_residual: Decimal
    payments_applied: Decimal
    credits_applied: Decimal
    opening_funding_applied: Decimal
    canonical_discharge: Decimal
    residual: Decimal
    created_at: datetime
    updated_at: datetime
    collections_due_basis_verified: bool
    line_ids: tuple[UUID, ...]
    settlement_ids: tuple[UUID, ...]


@dataclass(frozen=True)
class OpeningException:
    code: ExceptionCode
    account_id: UUID | None
    currency: str | None
    source_id: UUID
    detail: str


@dataclass(frozen=True)
class OpeningExclusion:
    source_id: UUID
    reason: ExclusionReason


@dataclass(frozen=True)
class LaneEvidence:
    account_id: UUID
    currency: str
    authority: BillingRecordAuthority
    authority_cutover_id: UUID | None
    fingerprint: str
    posting_group_count: int
    collectible_receivable: Decimal
    unapplied_customer_credit: Decimal
    prepaid_funding_reserved: Decimal
    prepaid_funding_consumed: Decimal
    written_off_total: Decimal
    refunded_total: Decimal
    adjustment_total: Decimal


@dataclass(frozen=True)
class PositionEffectEvidence:
    id: UUID
    group_id: UUID
    effect: str
    amount: Decimal
    currency: str
    obligation_id: UUID | None
    invoice_id: UUID | None
    payment_id: UUID | None
    credit_note_id: UUID | None
    entitlement_id: UUID | None


@dataclass(frozen=True)
class OpeningControl:
    account_id: UUID | None
    currency: str | None
    invoice_count: int
    eligible_count: int
    excluded_count: int
    exception_count: int
    original_total: Decimal
    canonical_discharge: Decimal
    residual: Decimal
    eligible_residual: Decimal
    source_count: int


@dataclass(frozen=True)
class BillingOpeningExportPreview:
    capture: CaptureEvidence
    subscriber_ids: tuple[UUID, ...]
    invoices: tuple[InvoiceOpeningEvidence, ...]
    lines: tuple[InvoiceLineEvidence, ...]
    settlements: tuple[SettlementEvidence, ...]
    financial_sources: tuple[FinancialSourceEvidence, ...]
    lanes: tuple[LaneEvidence, ...]
    effects: tuple[PositionEffectEvidence, ...]
    eligible_invoice_ids: tuple[UUID, ...]
    exclusions: tuple[OpeningExclusion, ...]
    blocking_exceptions: tuple[OpeningException, ...]
    controls: tuple[OpeningControl, ...]
    content_fingerprint: str


# V2 raw receipt observations. No record attests external cash or source namespace.


@dataclass(frozen=True)
class CashPaymentEvidence:
    id: UUID
    account_id: UUID | None
    billing_account_id: UUID | None
    provider_id: UUID | None
    external_id: str | None
    amount: Decimal
    provider_fee: Decimal
    refunded_amount: Decimal
    currency: str
    status: PaymentStatus
    is_active: bool
    paid_at: datetime | None
    reserved_for_purchase_id: UUID | None
    import_run_id: UUID | None
    created_at: datetime
    updated_at: datetime


@dataclass(frozen=True)
class CashSettlementEvidence:
    id: UUID
    payment_id: UUID
    origin: PaymentSettlementOrigin
    amount: Decimal
    unallocated_amount: Decimal
    prepaid_amount: Decimal
    currency: str
    unallocated_ledger_entry_id: UUID | None
    prepaid_ledger_entry_id: UUID | None
    billing_account_ledger_entry_id: UUID | None
    preview_fingerprint: str | None
    idempotency_key: str | None
    created_at: datetime


@dataclass(frozen=True)
class CashProviderEventEvidence:
    id: UUID
    provider_id: UUID
    payment_id: UUID | None
    invoice_id: UUID | None
    external_id: str | None
    event_type: str
    idempotency_key: str | None
    source: PaymentProviderEventSource
    status: PaymentProviderEventStatus
    observed_payment_status: PaymentStatus | None
    amount: Decimal | None
    provider_fee: Decimal
    net_amount: Decimal | None
    currency: str | None
    provider_reference: str | None
    financial_effect: PaymentProviderEventFinancialEffect
    observation_digest: str | None
    error_code: str | None
    received_at: datetime
    processed_at: datetime | None


@dataclass(frozen=True)
class CashRefundEvidence:
    id: UUID
    payment_id: UUID
    provider_event_id: UUID | None
    ledger_entry_id: UUID | None
    billing_account_ledger_entry_id: UUID | None
    credit_consumption_ledger_entry_id: UUID | None
    amount: Decimal
    currency: str
    origin: PaymentRefundOrigin
    preview_fingerprint: str | None
    created_at: datetime


@dataclass(frozen=True)
class CashReversalEvidence:
    id: UUID
    payment_id: UUID
    provider_event_id: UUID | None
    ledger_entry_id: UUID | None
    billing_account_ledger_entry_id: UUID | None
    credit_consumption_ledger_entry_id: UUID | None
    amount: Decimal
    currency: str
    origin: PaymentReversalOrigin
    preview_fingerprint: str | None
    created_at: datetime


@dataclass(frozen=True)
class CashProofEvidence:
    id: UUID
    account_id: UUID | None
    billing_account_id: UUID | None
    payment_id: UUID | None
    amount: Decimal
    gross_amount: Decimal | None
    wht_amount: Decimal | None
    verified_amount: Decimal | None
    currency: str
    status: PaymentProofStatus
    paid_at: datetime | None
    created_at: datetime
    updated_at: datetime


@dataclass(frozen=True)
class CashProofCorrectionEvidence:
    id: UUID
    duplicate_proof_id: UUID
    original_proof_id: UUID
    duplicate_payment_id: UUID
    payment_reversal_id: UUID
    ledger_entry_id: UUID
    preview_fingerprint: str
    idempotency_key: str
    created_at: datetime


@dataclass(frozen=True)
class CashTopupEvidence:
    id: UUID
    account_id: UUID | None
    billing_account_id: UUID | None
    invoice_id: UUID | None
    completed_payment_id: UUID | None
    provider_id: UUID | None
    capability_binding_id: UUID | None
    currency: str
    requested_amount: Decimal
    actual_amount: Decimal | None
    external_id: str | None
    status: str
    completed_at: datetime | None
    created_at: datetime
    updated_at: datetime


@dataclass(frozen=True)
class CashInboxEvidence:
    id: UUID
    installation_id: UUID
    capability_binding_id: UUID
    provider_event_id: str
    event_type: str
    payload_digest: str
    state: str
    consequence_provider_event_id: UUID | None
    consequence_payment_id: UUID | None
    consequence_provider_id: UUID | None
    consequence_identifiers_valid: bool
    error_code: str | None
    received_at: datetime
    processed_at: datetime | None
    created_at: datetime
    updated_at: datetime


class CashSourceKind(StrEnum):
    payments = "payments"
    payment_settlements = "payment_settlements"
    payment_provider_events = "payment_provider_events"
    integration_inbox = "integration_inbox"
    payment_refunds = "payment_refunds"
    payment_reversals = "payment_reversals"
    payment_proofs = "payment_proofs"
    payment_proof_corrections = "payment_proof_corrections"
    topup_intents = "topup_intents"


@dataclass(frozen=True)
class CashSourceCount:
    table: CashSourceKind
    count: int


@dataclass(frozen=True)
class CashEvidencePopulation:
    payments: tuple[CashPaymentEvidence, ...]
    payment_settlements: tuple[CashSettlementEvidence, ...]
    provider_events: tuple[CashProviderEventEvidence, ...]
    inbox_receipts: tuple[CashInboxEvidence, ...]
    refunds: tuple[CashRefundEvidence, ...]
    reversals: tuple[CashReversalEvidence, ...]
    proofs: tuple[CashProofEvidence, ...]
    proof_corrections: tuple[CashProofCorrectionEvidence, ...]
    topups: tuple[CashTopupEvidence, ...]
    source_counts: tuple[CashSourceCount, ...]
    blocking_exceptions: tuple[OpeningException, ...]
    # No source verifier or namespace mapper is installed on this query.
    qualified_payment_ids: tuple[UUID, ...] = ()


@dataclass(frozen=True)
class BillingOpeningExportPreviewV2(BillingOpeningExportPreview):
    cash_evidence: CashEvidencePopulation


# V3 observes persisted current columns, never an original issued document.
@dataclass(frozen=True)
class CurrentInvoiceDocumentObservation:
    invoice_id: UUID
    subtotal: Decimal
    discount_amount: Decimal
    tax_total: Decimal
    total: Decimal
    billing_period_start: datetime | None
    billing_period_end: datetime | None


@dataclass(frozen=True)
class CurrentInvoiceLineTaxObservation:
    line_id: UUID
    invoice_id: UUID
    description: str
    tax_rate_id: UUID | None
    tax_rate_snapshot_version: int | None
    tax_rate_code_snapshot: str | None
    tax_rate_percent_snapshot: Decimal | None
    tax_rate_is_active_snapshot: bool | None
    tax_application: TaxApplication


class OriginalEvidenceRequirement(StrEnum):
    document_number = "document_number"
    issued_at = "issued_at"
    documentary_reference = "documentary_reference"
    documentary_digest = "documentary_digest"
    subtotal = "subtotal"
    tax_total = "tax_total"
    grand_total = "grand_total"
    seller = "seller"
    customer = "customer"
    party_tax_identities = "party_tax_identities"
    tax_snapshots = "tax_snapshots"
    tax_evidence_ref = "tax_evidence_ref"
    fx_evidence_ref = "fx_evidence_ref"
    document_profile_code = "document_profile_code"
    document_profile_version = "document_profile_version"
    payment_instructions = "payment_instructions"
    brand_asset = "brand_asset"
    locale = "locale"
    timezone = "timezone"
    historical_discharge_source_mapping = "historical_discharge_source_mapping"
    historical_discharge_tax_evidence = "historical_discharge_tax_evidence"
    withholding_non_cash_replay_bindings = "withholding_non_cash_replay_bindings"


class OriginalEvidenceUnavailableReason(StrEnum):
    no_authenticated_frozen_document = "no_authenticated_frozen_document"
    no_qualified_discharge_replay = "no_qualified_discharge_replay"


@dataclass(frozen=True)
class UnavailableOriginalInvoiceEvidence:
    invoice_id: UUID
    requirements: tuple[OriginalEvidenceRequirement, ...]
    reasons: tuple[OriginalEvidenceUnavailableReason, ...]


@dataclass(frozen=True)
class DocumentaryObservationPopulation:
    current_invoices: tuple[CurrentInvoiceDocumentObservation, ...]
    current_lines: tuple[CurrentInvoiceLineTaxObservation, ...]
    # Every invoice, including excluded/closed rows, retains its evidence gaps.
    unavailable_original_evidence: tuple[UnavailableOriginalInvoiceEvidence, ...]


@dataclass(frozen=True)
class BillingOpeningExportPreviewV3(BillingOpeningExportPreviewV2):
    documentary_observations: DocumentaryObservationPopulation
