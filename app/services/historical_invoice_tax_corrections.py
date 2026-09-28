"""Reviewed atomic correction for a paid invoice that omitted output tax.

The owner preserves history: it voids the incorrect paid document through the
invoice owner, releases the exact native payment allocation, settles one named
subscription draft, creates a tax-correct replacement, and consumes the same
payment completely.  Preview is read-only; confirmation is single-account,
fingerprinted, idempotent, and all-or-nothing.
"""

from __future__ import annotations

import enum
import hashlib
import json
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
from typing import NoReturn
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.audit import AuditActorType
from app.models.billing import (
    Invoice,
    InvoiceClosure,
    InvoiceClosureType,
    InvoiceDueDateBasis,
    InvoiceLine,
    InvoiceStatus,
    LedgerEntry,
    LedgerEntryType,
    LedgerSource,
    Payment,
    PaymentAllocation,
    PaymentRefund,
    PaymentReversal,
    PaymentSettlement,
    PaymentStatus,
    TaxApplication,
    TaxRate,
)
from app.schemas.audit import AuditEventCreate
from app.schemas.billing import (
    InvoiceCreate,
    PaymentSettlementReconciliationRequest,
    SystemInvoiceLineCreate,
)
from app.services.audit import AuditEvents
from app.services.billing._common import (
    get_spendable_account_credit_balance,
    lock_account,
)
from app.services.billing.account_credit import (
    AccountCreditApplicationError,
    AccountCreditApplications,
)
from app.services.billing.invoices import (
    ExistingInvoiceTaxReplacementEvidence,
    HistoricalInvoiceTaxCorrectionDocumentEvidence,
    InvoiceIssuanceInput,
    InvoiceLines,
    InvoiceOwnerError,
    Invoices,
)
from app.services.billing.payments import (
    PaymentAllocations,
    Payments,
    ReviewedLegacyAllocationConsumptionEvidence,
)
from app.services.common import round_money
from app.services.customer_tax_policies import get_customer_vat_exemption_policy
from app.services.domain_errors import DomainError
from app.services.events import emit_event
from app.services.events.types import EventType
from app.services.locking import lock_for_update
from app.services.owner_commands import (
    CommandContext,
    OwnerCommandDefinition,
    execute_owner_command,
)

OWNER = "financial.historical_invoice_tax_corrections"
CONCERN = "reviewed historical invoice tax correction coordination"
CORRECTION_SCOPE = "billing:invoice:update"
_POLICY_VERSION = "historical-invoice-tax-correction-v1"
_MAX_REASON_LENGTH = 500

_CORRECT_COMMAND = OwnerCommandDefinition(
    owner=OWNER,
    concern=CONCERN,
    name="correct_historical_invoice_tax",
)


class HistoricalInvoiceTaxCorrectionDisposition(enum.StrEnum):
    eligible = "eligible"
    already_corrected = "already_corrected"
    manual_review = "manual_review"


class HistoricalInvoiceTaxCorrectionError(DomainError):
    """Transport-neutral rejection from the correction owner."""


def _error(suffix: str, message: str, **details: object) -> NoReturn:
    raise HistoricalInvoiceTaxCorrectionError(
        code=f"{OWNER}.{suffix}",
        message=message,
        details=details,
    )


@dataclass(frozen=True, slots=True)
class HistoricalInvoiceTaxCorrectionQuery:
    account_id: UUID
    source_invoice_id: UUID
    source_invoice_line_id: UUID
    void_evidence_invoice_id: UUID
    subscription_invoice_id: UUID
    payment_id: UUID
    tax_rate_id: UUID
    issued_at: datetime
    due_at: datetime
    currency: str = "NGN"


@dataclass(frozen=True, slots=True)
class HistoricalInvoiceTaxCorrectionPreview:
    disposition: HistoricalInvoiceTaxCorrectionDisposition
    reason: str
    account_id: UUID
    source_invoice_id: UUID
    source_invoice_number: str | None
    void_evidence_invoice_id: UUID
    subscription_invoice_id: UUID
    subscription_invoice_number: str | None
    payment_id: UUID
    tax_rate_id: UUID
    currency: str
    source_subtotal: Decimal
    source_tax_total: Decimal
    subscription_total: Decimal
    tax_rate_percent: Decimal
    tax_amount: Decimal
    replacement_total: Decimal
    payment_amount: Decimal
    payment_available_before: Decimal
    payment_available_after_void: Decimal
    projected_final_payment_available: Decimal
    account_credit_before: Decimal
    projected_final_account_credit: Decimal
    source_void_fingerprint: str | None
    source_payment_allocation_id: UUID | None
    replacement_invoice_id: UUID | None
    fingerprint: str

    @property
    def actionable(self) -> bool:
        return self.disposition is HistoricalInvoiceTaxCorrectionDisposition.eligible


@dataclass(frozen=True, slots=True)
class CorrectHistoricalInvoiceTaxCommand:
    query: HistoricalInvoiceTaxCorrectionQuery
    expected_preview_fingerprint: str
    permission_granted: bool
    authorized_system_user_id: UUID


@dataclass(frozen=True, slots=True)
class HistoricalInvoiceTaxCorrectionResult:
    account_id: UUID
    source_invoice_id: UUID
    source_invoice_closure_id: UUID
    source_payment_allocation_id: UUID
    subscription_invoice_id: UUID
    replacement_invoice_id: UUID
    payment_id: UUID
    subscription_payment_allocation_id: UUID
    replacement_payment_allocation_id: UUID
    source_subtotal: Decimal
    subscription_total: Decimal
    tax_amount: Decimal
    replacement_total: Decimal
    currency: str
    preview_fingerprint: str
    replayed: bool


def _utc(value: datetime) -> datetime:
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


def _normalized_currency(value: str) -> str:
    currency = value.strip().upper()
    if len(currency) != 3 or not currency.isalpha():
        _error("currency_invalid", "Correction currency must be a three-letter code.")
    return currency


def _normalized_reason(value: str) -> str:
    reason = value.strip()
    if not reason or len(reason) > _MAX_REASON_LENGTH:
        _error(
            "reason_invalid",
            f"Correction reason must contain 1 to {_MAX_REASON_LENGTH} characters.",
        )
    return reason


def _fingerprint(payload: dict[str, object]) -> str:
    encoded = json.dumps(
        payload,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _child_key(kind: str, idempotency_key: str) -> str:
    digest = hashlib.sha256(f"{kind}:{idempotency_key}".encode()).hexdigest()
    return f"hist-tax-{kind}-{digest[:48]}"


def _active_lines(
    db: Session, invoice_id: UUID, *, lock: bool
) -> tuple[InvoiceLine, ...]:
    statement = (
        select(InvoiceLine)
        .where(
            InvoiceLine.invoice_id == invoice_id,
            InvoiceLine.is_active.is_(True),
            InvoiceLine.amount > Decimal("0.00"),
        )
        .order_by(InvoiceLine.id)
    )
    if lock:
        statement = statement.with_for_update()
    return tuple(db.scalars(statement).all())


def _manual_preview(
    query: HistoricalInvoiceTaxCorrectionQuery,
    *,
    reason: str,
    source_invoice: Invoice | None = None,
    subscription_invoice: Invoice | None = None,
    replacement_invoice_id: UUID | None = None,
) -> HistoricalInvoiceTaxCorrectionPreview:
    currency = _normalized_currency(query.currency)
    payload: dict[str, object] = {
        "disposition": HistoricalInvoiceTaxCorrectionDisposition.manual_review.value,
        "reason": reason,
        "account_id": query.account_id,
        "source_invoice_id": query.source_invoice_id,
        "void_evidence_invoice_id": query.void_evidence_invoice_id,
        "subscription_invoice_id": query.subscription_invoice_id,
        "payment_id": query.payment_id,
        "tax_rate_id": query.tax_rate_id,
        "issued_at": _utc(query.issued_at),
        "due_at": _utc(query.due_at),
        "currency": currency,
    }
    zero = Decimal("0.00")
    return HistoricalInvoiceTaxCorrectionPreview(
        disposition=HistoricalInvoiceTaxCorrectionDisposition.manual_review,
        reason=reason,
        account_id=query.account_id,
        source_invoice_id=query.source_invoice_id,
        source_invoice_number=(
            source_invoice.invoice_number if source_invoice is not None else None
        ),
        void_evidence_invoice_id=query.void_evidence_invoice_id,
        subscription_invoice_id=query.subscription_invoice_id,
        subscription_invoice_number=(
            subscription_invoice.invoice_number
            if subscription_invoice is not None
            else None
        ),
        payment_id=query.payment_id,
        tax_rate_id=query.tax_rate_id,
        currency=currency,
        source_subtotal=zero,
        source_tax_total=zero,
        subscription_total=zero,
        tax_rate_percent=zero,
        tax_amount=zero,
        replacement_total=zero,
        payment_amount=zero,
        payment_available_before=zero,
        payment_available_after_void=zero,
        projected_final_payment_available=zero,
        account_credit_before=zero,
        projected_final_account_credit=zero,
        source_void_fingerprint=None,
        source_payment_allocation_id=None,
        replacement_invoice_id=replacement_invoice_id,
        fingerprint=_fingerprint(payload),
    )


def _existing_replacement(
    db: Session, query: HistoricalInvoiceTaxCorrectionQuery
) -> tuple[Invoice, HistoricalInvoiceTaxCorrectionDocumentEvidence] | None:
    invoices = db.scalars(
        select(Invoice).where(
            Invoice.account_id == query.account_id,
            Invoice.is_active.is_(True),
        )
    ).all()
    for invoice in invoices:
        evidence = Invoices.historical_tax_correction_evidence(invoice)
        if evidence is None or evidence.source_invoice_id != query.source_invoice_id:
            continue
        if (
            evidence.account_id != query.account_id
            or evidence.source_invoice_line_id != query.source_invoice_line_id
            or evidence.void_evidence_invoice_id != query.void_evidence_invoice_id
            or evidence.subscription_invoice_id != query.subscription_invoice_id
            or evidence.payment_id != query.payment_id
            or evidence.tax_rate_id != query.tax_rate_id
        ):
            _error(
                "existing_correction_conflict",
                "Source invoice already has different correction evidence.",
                source_invoice_id=str(query.source_invoice_id),
            )
        return invoice, evidence
    return None


def _already_corrected_preview(
    query: HistoricalInvoiceTaxCorrectionQuery,
    *,
    replacement: Invoice,
    evidence: HistoricalInvoiceTaxCorrectionDocumentEvidence,
) -> HistoricalInvoiceTaxCorrectionPreview:
    source = replacement.account_id == query.account_id
    if (
        not source
        or replacement.status is not InvoiceStatus.paid
        or round_money(replacement.balance_due) != Decimal("0.00")
        or round_money(replacement.total) != round_money(evidence.replacement_total)
    ):
        _error(
            "existing_correction_drift",
            "Existing correction replacement invoice has drifted.",
            replacement_invoice_id=str(replacement.id),
        )
    zero = Decimal("0.00")
    return HistoricalInvoiceTaxCorrectionPreview(
        disposition=HistoricalInvoiceTaxCorrectionDisposition.already_corrected,
        reason="The exact historical tax correction is already complete.",
        account_id=query.account_id,
        source_invoice_id=query.source_invoice_id,
        source_invoice_number=None,
        void_evidence_invoice_id=query.void_evidence_invoice_id,
        subscription_invoice_id=query.subscription_invoice_id,
        subscription_invoice_number=None,
        payment_id=query.payment_id,
        tax_rate_id=query.tax_rate_id,
        currency=evidence.currency,
        source_subtotal=evidence.source_subtotal,
        source_tax_total=zero,
        subscription_total=evidence.subscription_total,
        tax_rate_percent=zero,
        tax_amount=evidence.tax_amount,
        replacement_total=evidence.replacement_total,
        payment_amount=round_money(
            evidence.subscription_total + evidence.replacement_total
        ),
        payment_available_before=zero,
        payment_available_after_void=zero,
        projected_final_payment_available=zero,
        account_credit_before=zero,
        projected_final_account_credit=zero,
        source_void_fingerprint=None,
        source_payment_allocation_id=evidence.source_payment_allocation_id,
        replacement_invoice_id=replacement.id,
        fingerprint=evidence.preview_fingerprint,
    )


def _build_preview(
    db: Session,
    query: HistoricalInvoiceTaxCorrectionQuery,
    *,
    lock: bool,
) -> HistoricalInvoiceTaxCorrectionPreview:
    currency = _normalized_currency(query.currency)
    issued_at = _utc(query.issued_at)
    due_at = _utc(query.due_at)
    if due_at < issued_at:
        return _manual_preview(
            query, reason="Replacement due date precedes issue date."
        )

    existing = _existing_replacement(db, query)
    if existing is not None:
        _result_from_replacement(
            db,
            replacement=existing[0],
            expected_fingerprint=existing[1].preview_fingerprint,
            replayed=True,
        )
        return _already_corrected_preview(
            query,
            replacement=existing[0],
            evidence=existing[1],
        )

    if get_customer_vat_exemption_policy(
        db,
        account_id=query.account_id,
    ).vat_exempt:
        return _manual_preview(
            query,
            reason="Customer is currently VAT-exempt and requires renewed review.",
        )

    records: dict[UUID, Invoice | None] = {}
    for invoice_id in sorted(
        {
            query.source_invoice_id,
            query.void_evidence_invoice_id,
            query.subscription_invoice_id,
        },
        key=str,
    ):
        records[invoice_id] = (
            lock_for_update(db, Invoice, invoice_id)
            if lock
            else db.get(Invoice, invoice_id)
        )
    source_invoice = records[query.source_invoice_id]
    void_evidence = records[query.void_evidence_invoice_id]
    subscription_invoice = records[query.subscription_invoice_id]
    if source_invoice is None or void_evidence is None or subscription_invoice is None:
        return _manual_preview(
            query,
            reason="One or more reviewed invoice documents were not found.",
            source_invoice=source_invoice,
            subscription_invoice=subscription_invoice,
        )
    if len({invoice.id for invoice in records.values() if invoice is not None}) != 3:
        return _manual_preview(
            query,
            reason="Source, void evidence, and subscription invoices must be distinct.",
            source_invoice=source_invoice,
            subscription_invoice=subscription_invoice,
        )
    documents = (source_invoice, void_evidence, subscription_invoice)
    if any(
        invoice.account_id != query.account_id
        or invoice.currency.upper() != currency
        or not invoice.is_active
        or invoice.is_proforma
        for invoice in documents
    ):
        return _manual_preview(
            query,
            reason="Reviewed invoice account, currency, or lifecycle evidence differs.",
            source_invoice=source_invoice,
            subscription_invoice=subscription_invoice,
        )

    source_lines = _active_lines(db, source_invoice.id, lock=lock)
    if (
        source_invoice.status is not InvoiceStatus.paid
        or round_money(source_invoice.balance_due) != Decimal("0.00")
        or round_money(source_invoice.tax_total) != Decimal("0.00")
        or len(source_lines) != 1
        or source_lines[0].id != query.source_invoice_line_id
        or source_lines[0].tax_rate_id is not None
        or round_money(source_lines[0].amount) != round_money(source_invoice.subtotal)
        or round_money(source_invoice.subtotal) != round_money(source_invoice.total)
        or round_money(source_invoice.total) <= Decimal("0.00")
    ):
        return _manual_preview(
            query,
            reason="Source invoice is not an exact paid base-only document.",
            source_invoice=source_invoice,
            subscription_invoice=subscription_invoice,
        )

    source_allocations_stmt = select(PaymentAllocation).where(
        PaymentAllocation.invoice_id == source_invoice.id,
        PaymentAllocation.is_active.is_(True),
        PaymentAllocation.amount > Decimal("0.00"),
    )
    if lock:
        source_allocations_stmt = source_allocations_stmt.with_for_update()
    source_allocations = tuple(db.scalars(source_allocations_stmt).all())
    if (
        len(source_allocations) != 1
        or source_allocations[0].payment_id != query.payment_id
        or round_money(source_allocations[0].amount)
        != round_money(source_invoice.total)
        or source_allocations[0].ledger_entry_id is None
        or source_allocations[0].consumption_ledger_entry_id is None
    ):
        return _manual_preview(
            query,
            reason="Source invoice is not funded by one exact releasable allocation.",
            source_invoice=source_invoice,
            subscription_invoice=subscription_invoice,
        )
    try:
        void_preview = Invoices.preview_void_for_owner(db, source_invoice.id)
    except InvoiceOwnerError:
        return _manual_preview(
            query,
            reason="Invoice owner cannot safely void and release the source invoice.",
            source_invoice=source_invoice,
            subscription_invoice=subscription_invoice,
        )
    if (
        void_preview.released_allocation_ids != (source_allocations[0].id,)
        or round_money(void_preview.payments_applied)
        != round_money(source_invoice.total)
        or round_money(void_preview.credits_applied) != Decimal("0.00")
    ):
        return _manual_preview(
            query,
            reason="Source invoice void preview does not release the exact payment.",
            source_invoice=source_invoice,
            subscription_invoice=subscription_invoice,
        )

    payment = (
        lock_for_update(db, Payment, query.payment_id)
        if lock
        else db.get(Payment, query.payment_id)
    )
    if payment is None:
        return _manual_preview(
            query,
            reason="Reviewed payment was not found.",
            source_invoice=source_invoice,
            subscription_invoice=subscription_invoice,
        )

    evidence_lines = _active_lines(db, void_evidence.id, lock=lock)
    evidence_closure = db.scalar(
        select(InvoiceClosure).where(InvoiceClosure.invoice_id == void_evidence.id)
    )
    tax_rate = (
        lock_for_update(db, TaxRate, query.tax_rate_id)
        if lock
        else db.get(TaxRate, query.tax_rate_id)
    )
    source_subtotal = round_money(source_invoice.subtotal)
    if tax_rate is None or not tax_rate.is_active or round_money(tax_rate.rate) <= 0:
        return _manual_preview(
            query,
            reason="Reviewed tax rate is missing or inactive.",
            source_invoice=source_invoice,
            subscription_invoice=subscription_invoice,
        )
    tax_percent = Decimal(str(tax_rate.rate))
    tax_amount = round_money(source_subtotal * tax_percent / Decimal("100"))
    replacement_total = round_money(source_subtotal + tax_amount)
    if (
        void_evidence.status is not InvoiceStatus.void
        or len(evidence_lines) != 1
        or evidence_lines[0].tax_rate_id != tax_rate.id
        or evidence_lines[0].tax_application is not TaxApplication.exclusive
        or round_money(evidence_lines[0].amount) != source_subtotal
        or evidence_lines[0].tax_rate_snapshot_version != 1
        or evidence_lines[0].tax_rate_code_snapshot != tax_rate.code
        or evidence_lines[0].tax_rate_percent_snapshot != tax_rate.rate
        or evidence_lines[0].tax_rate_is_active_snapshot is not True
        or round_money(void_evidence.subtotal) != source_subtotal
        or round_money(void_evidence.tax_total) != tax_amount
        or round_money(void_evidence.total) != replacement_total
        or evidence_closure is None
        or evidence_closure.closure_type is not InvoiceClosureType.void
        or round_money(evidence_closure.payments_applied) != Decimal("0.00")
        or round_money(evidence_closure.credits_applied) != Decimal("0.00")
    ):
        return _manual_preview(
            query,
            reason="Voided Finance draft does not prove the exact replacement tax shape.",
            source_invoice=source_invoice,
            subscription_invoice=subscription_invoice,
        )

    subscription_lines = _active_lines(db, subscription_invoice.id, lock=lock)
    subscription_allocations = tuple(
        db.scalars(
            select(PaymentAllocation).where(
                PaymentAllocation.invoice_id == subscription_invoice.id,
                PaymentAllocation.is_active.is_(True),
                PaymentAllocation.amount > Decimal("0.00"),
            )
        ).all()
    )
    subscription_total = round_money(subscription_invoice.total)
    subscription_subtotal = round_money(subscription_invoice.subtotal)
    subscription_tax = round_money(subscription_subtotal * tax_percent / Decimal("100"))
    if (
        subscription_invoice.status is not InvoiceStatus.draft
        or round_money(subscription_invoice.balance_due) != subscription_total
        or subscription_total <= Decimal("0.00")
        or len(subscription_lines) != 1
        or round_money(subscription_lines[0].amount) != subscription_subtotal
        or subscription_lines[0].tax_rate_id != tax_rate.id
        or subscription_lines[0].tax_application is not TaxApplication.exclusive
        or subscription_lines[0].tax_rate_snapshot_version != 1
        or subscription_lines[0].tax_rate_code_snapshot != tax_rate.code
        or subscription_lines[0].tax_rate_percent_snapshot != tax_rate.rate
        or subscription_lines[0].tax_rate_is_active_snapshot is not True
        or round_money(subscription_invoice.tax_total) != subscription_tax
        or subscription_total != round_money(subscription_subtotal + subscription_tax)
        or subscription_allocations
    ):
        return _manual_preview(
            query,
            reason="Subscription invoice is not one pristine positive draft.",
            source_invoice=source_invoice,
            subscription_invoice=subscription_invoice,
        )

    has_return = bool(
        db.scalar(
            select(PaymentRefund.id).where(PaymentRefund.payment_id == payment.id)
        )
        or db.scalar(
            select(PaymentReversal.id).where(PaymentReversal.payment_id == payment.id)
        )
    )
    all_allocations = tuple(
        db.scalars(
            select(PaymentAllocation).where(
                PaymentAllocation.payment_id == payment.id,
                PaymentAllocation.is_active.is_(True),
                PaymentAllocation.amount > Decimal("0.00"),
            )
        ).all()
    )
    payment_amount = round_money(payment.amount)
    payment_available = round_money(
        PaymentAllocations.available_amount(db, str(payment.id))
    )
    account_credit = round_money(
        get_spendable_account_credit_balance(
            db,
            str(query.account_id),
            currency=currency,
        )
    )
    required_available = round_money(subscription_total + tax_amount)
    if (
        not payment.is_active
        or payment.status is not PaymentStatus.succeeded
        or payment.account_id != query.account_id
        or payment.currency.upper() != currency
        or round_money(payment.refunded_amount) != Decimal("0.00")
        or has_return
        or {allocation.id for allocation in all_allocations}
        != {allocation.id for allocation in source_allocations}
        or payment_amount != round_money(subscription_total + replacement_total)
        or payment_available != required_available
        or account_credit != required_available
    ):
        return _manual_preview(
            query,
            reason="Named payment is not the exact sole funding source for both documents.",
            source_invoice=source_invoice,
            subscription_invoice=subscription_invoice,
        )

    payload: dict[str, object] = {
        "disposition": HistoricalInvoiceTaxCorrectionDisposition.eligible.value,
        "account_id": query.account_id,
        "source_invoice_id": source_invoice.id,
        "source_invoice_updated_at": _utc(source_invoice.updated_at),
        "source_invoice_line_id": source_lines[0].id,
        "source_invoice_line_updated_at": _utc(source_lines[0].updated_at),
        "source_payment_allocation_id": source_allocations[0].id,
        "void_evidence_invoice_id": void_evidence.id,
        "void_evidence_updated_at": _utc(void_evidence.updated_at),
        "subscription_invoice_id": subscription_invoice.id,
        "subscription_invoice_updated_at": _utc(subscription_invoice.updated_at),
        "payment_id": payment.id,
        "payment_updated_at": _utc(payment.updated_at),
        "tax_rate_id": tax_rate.id,
        "tax_rate_updated_at": _utc(tax_rate.updated_at),
        "currency": currency,
        "source_subtotal": source_subtotal,
        "subscription_total": subscription_total,
        "tax_rate_percent": tax_percent,
        "tax_amount": tax_amount,
        "replacement_total": replacement_total,
        "payment_amount": payment_amount,
        "payment_available_before": payment_available,
        "account_credit_before": account_credit,
        "source_void_fingerprint": void_preview.fingerprint,
        "issued_at": issued_at,
        "due_at": due_at,
    }
    return HistoricalInvoiceTaxCorrectionPreview(
        disposition=HistoricalInvoiceTaxCorrectionDisposition.eligible,
        reason=(
            "The named payment exactly funds the subscription and VAT-correct "
            "replacement after the source allocation is released."
        ),
        account_id=query.account_id,
        source_invoice_id=source_invoice.id,
        source_invoice_number=source_invoice.invoice_number,
        void_evidence_invoice_id=void_evidence.id,
        subscription_invoice_id=subscription_invoice.id,
        subscription_invoice_number=subscription_invoice.invoice_number,
        payment_id=payment.id,
        tax_rate_id=tax_rate.id,
        currency=currency,
        source_subtotal=source_subtotal,
        source_tax_total=Decimal("0.00"),
        subscription_total=subscription_total,
        tax_rate_percent=tax_percent,
        tax_amount=tax_amount,
        replacement_total=replacement_total,
        payment_amount=payment_amount,
        payment_available_before=payment_available,
        payment_available_after_void=payment_amount,
        projected_final_payment_available=Decimal("0.00"),
        account_credit_before=account_credit,
        projected_final_account_credit=Decimal("0.00"),
        source_void_fingerprint=void_preview.fingerprint,
        source_payment_allocation_id=source_allocations[0].id,
        replacement_invoice_id=None,
        fingerprint=_fingerprint(payload),
    )


def preview_historical_invoice_tax_correction(
    db: Session,
    query: HistoricalInvoiceTaxCorrectionQuery,
) -> HistoricalInvoiceTaxCorrectionPreview:
    """Return the exact no-write correction decision."""

    return _build_preview(db, query, lock=False)


def _result_from_replacement(
    db: Session,
    *,
    replacement: Invoice,
    expected_fingerprint: str,
    replayed: bool,
) -> HistoricalInvoiceTaxCorrectionResult:
    evidence = Invoices.historical_tax_correction_evidence(replacement)
    if evidence is None or evidence.preview_fingerprint != expected_fingerprint:
        _error(
            "replay_conflict",
            "Correction idempotency evidence belongs to a different preview.",
            replacement_invoice_id=str(replacement.id),
        )
    source = db.get(Invoice, evidence.source_invoice_id)
    source_line = db.get(InvoiceLine, evidence.source_invoice_line_id)
    void_evidence = db.get(Invoice, evidence.void_evidence_invoice_id)
    subscription = db.get(Invoice, evidence.subscription_invoice_id)
    payment = db.get(Payment, evidence.payment_id)
    closure = db.get(InvoiceClosure, evidence.source_invoice_closure_id)
    replacement_lines = _active_lines(db, replacement.id, lock=False)
    subscription_allocation = db.get(
        PaymentAllocation, evidence.subscription_payment_allocation_id
    )
    replacement_allocation = db.get(
        PaymentAllocation, evidence.replacement_payment_allocation_id
    )
    source_allocation = db.get(PaymentAllocation, evidence.source_payment_allocation_id)
    if (
        source is None
        or source.status is not InvoiceStatus.void
        or round_money(source.total) != round_money(evidence.source_subtotal)
        or source_line is None
        or not source_line.is_active
        or source_line.invoice_id != source.id
        or round_money(source_line.amount) != round_money(evidence.source_subtotal)
        or void_evidence is None
        or void_evidence.status is not InvoiceStatus.void
        or subscription is None
        or subscription.status is not InvoiceStatus.paid
        or round_money(subscription.total) != round_money(evidence.subscription_total)
        or replacement.status is not InvoiceStatus.paid
        or replacement.account_id != evidence.account_id
        or replacement.currency.upper() != evidence.currency
        or round_money(replacement.subtotal) != round_money(evidence.source_subtotal)
        or round_money(replacement.tax_total) != round_money(evidence.tax_amount)
        or round_money(replacement.total) != round_money(evidence.replacement_total)
        or len(replacement_lines) != 1
        or replacement_lines[0].tax_rate_id != evidence.tax_rate_id
        or replacement_lines[0].tax_application is not TaxApplication.exclusive
        or payment is None
        or not payment.is_active
        or payment.status is not PaymentStatus.succeeded
        or payment.account_id != evidence.account_id
        or payment.currency.upper() != evidence.currency
        or round_money(payment.amount)
        != round_money(evidence.subscription_total + evidence.replacement_total)
        or closure is None
        or closure.invoice_id != source.id
        or closure.closure_type is not InvoiceClosureType.void
        or round_money(closure.payments_applied)
        != round_money(evidence.source_subtotal)
        or source_allocation is None
        or source_allocation.is_active
        or source_allocation.payment_id != evidence.payment_id
        or source_allocation.invoice_id != source.id
        or round_money(source_allocation.amount)
        != round_money(evidence.source_subtotal)
        or subscription_allocation is None
        or not subscription_allocation.is_active
        or subscription_allocation.payment_id != evidence.payment_id
        or subscription_allocation.invoice_id != subscription.id
        or round_money(subscription_allocation.amount)
        != round_money(evidence.subscription_total)
        or replacement_allocation is None
        or not replacement_allocation.is_active
        or replacement_allocation.payment_id != evidence.payment_id
        or replacement_allocation.invoice_id != replacement.id
        or round_money(replacement_allocation.amount)
        != round_money(evidence.replacement_total)
        or round_money(
            PaymentAllocations.available_amount(db, str(evidence.payment_id))
        )
        != Decimal("0.00")
    ):
        _error(
            "correction_evidence_drift",
            "Historical tax correction evidence is incomplete or has drifted.",
            replacement_invoice_id=str(replacement.id),
        )
    return HistoricalInvoiceTaxCorrectionResult(
        account_id=evidence.account_id,
        source_invoice_id=evidence.source_invoice_id,
        source_invoice_closure_id=evidence.source_invoice_closure_id,
        source_payment_allocation_id=evidence.source_payment_allocation_id,
        subscription_invoice_id=evidence.subscription_invoice_id,
        replacement_invoice_id=replacement.id,
        payment_id=evidence.payment_id,
        subscription_payment_allocation_id=(
            evidence.subscription_payment_allocation_id
        ),
        replacement_payment_allocation_id=evidence.replacement_payment_allocation_id,
        source_subtotal=evidence.source_subtotal,
        subscription_total=evidence.subscription_total,
        tax_amount=evidence.tax_amount,
        replacement_total=evidence.replacement_total,
        currency=evidence.currency,
        preview_fingerprint=evidence.preview_fingerprint,
        replayed=replayed,
    )


def correct_historical_invoice_tax(
    db: Session,
    command: CorrectHistoricalInvoiceTaxCommand,
    *,
    context: CommandContext,
) -> HistoricalInvoiceTaxCorrectionResult:
    """Execute one reviewed correction as the only transaction owner."""

    return execute_owner_command(
        db,
        definition=_CORRECT_COMMAND,
        context=context,
        operation=lambda: _correct_historical_invoice_tax(
            db,
            command=command,
            context=context,
        ),
    )


def _correct_historical_invoice_tax(
    db: Session,
    *,
    command: CorrectHistoricalInvoiceTaxCommand,
    context: CommandContext,
) -> HistoricalInvoiceTaxCorrectionResult:
    key = (context.idempotency_key or "").strip()
    if len(key) < 16 or len(key) > 120:
        _error(
            "idempotency_key_required",
            "Correction idempotency key must contain 16 to 120 characters.",
        )
    reason = _normalized_reason(context.reason)
    if context.scope != CORRECTION_SCOPE:
        _error(
            "scope_invalid",
            "Correction command does not carry the invoice-update scope.",
        )
    if not command.permission_granted:
        _error("permission_denied", "Invoice correction permission is required.")
    if len(command.expected_preview_fingerprint) != 64:
        _error("preview_invalid", "A SHA-256 correction preview is required.")

    lock_account(db, str(command.query.account_id))
    existing = _existing_replacement(db, command.query)
    if existing is not None:
        return _result_from_replacement(
            db,
            replacement=existing[0],
            expected_fingerprint=command.expected_preview_fingerprint,
            replayed=True,
        )

    current = _build_preview(db, command.query, lock=True)
    if current.fingerprint != command.expected_preview_fingerprint:
        _error(
            "stale_preview",
            "Correction evidence changed after preview; preview again.",
            current_fingerprint=current.fingerprint,
        )
    if (
        not current.actionable
        or current.source_void_fingerprint is None
        or current.source_payment_allocation_id is None
    ):
        _error(
            "not_actionable",
            current.reason,
            disposition=current.disposition.value,
        )
    if get_customer_vat_exemption_policy(
        db,
        account_id=command.query.account_id,
    ).vat_exempt:
        _error(
            "customer_tax_policy_changed",
            "Customer is currently VAT-exempt; correction requires renewed review.",
        )

    source = db.get(Invoice, command.query.source_invoice_id)
    void_evidence = db.get(Invoice, command.query.void_evidence_invoice_id)
    subscription = db.get(Invoice, command.query.subscription_invoice_id)
    if source is None or void_evidence is None or subscription is None:
        _error("invoice_missing", "Reviewed invoice evidence disappeared under lock.")

    closure = Invoices.confirm_void_for_owner(
        db,
        source.id,
        preview_fingerprint=current.source_void_fingerprint,
        idempotency_key=_child_key("void", key),
        reason=reason,
        reconcile_access=False,
    ).closure

    subscription_issue = InvoiceIssuanceInput(
        issued_at=_utc(command.query.issued_at),
        due_at=_utc(command.query.due_at),
        due_date_basis=InvoiceDueDateBasis.approved_manual_override,
        due_date_basis_ref=f"historical-tax-correction:{source.id}:subscription",
        due_date_policy_version=_POLICY_VERSION,
        reason="historical_invoice_tax_correction_subscription_settlement",
    )
    Invoices.issue_draft_for_owner(
        db,
        str(subscription.id),
        issuance=subscription_issue,
        announce=False,
        apply_available_credit=False,
    )
    subscription_application = (
        AccountCreditApplications.apply_invoice_from_selected_payment_fully(
            db,
            subscription,
            payment_id=command.query.payment_id,
            expected_amount=current.subscription_total,
        )
    )
    if len(subscription_application.allocation_ids) != 1:
        _error(
            "subscription_settlement_incomplete",
            "Subscription invoice did not produce one exact payment allocation.",
        )
    subscription_allocation_id = UUID(subscription_application.allocation_ids[0])

    replacement = Invoices.stage_system_invoice_for_owner(
        db,
        InvoiceCreate(
            account_id=command.query.account_id,
            status=InvoiceStatus.draft,
            currency=current.currency,
            subtotal=Decimal("0.00"),
            tax_total=Decimal("0.00"),
            total=Decimal("0.00"),
            balance_due=Decimal("0.00"),
            memo=(
                f"VAT-correct replacement for "
                f"{source.invoice_number or source.id}; evidence "
                f"{void_evidence.invoice_number or void_evidence.id}"
            ),
        ),
        reason="historical_invoice_tax_correction_replacement",
    )
    evidence_line = _active_lines(db, void_evidence.id, lock=False)[0]
    InvoiceLines.stage_system_line_for_owner(
        db,
        SystemInvoiceLineCreate(
            invoice_id=replacement.id,
            description=evidence_line.description,
            quantity=evidence_line.quantity,
            unit_price=evidence_line.unit_price,
            amount=evidence_line.amount,
            tax_rate_id=command.query.tax_rate_id,
            tax_application=TaxApplication.exclusive,
            billing_line_key=f"historical-tax-correction:{source.id}",
            metadata_={
                "kind": "historical_invoice_tax_correction",
                "source_invoice_id": str(source.id),
                "void_evidence_invoice_id": str(void_evidence.id),
            },
        ),
        reason="historical_invoice_tax_correction_replacement",
    )
    replacement = Invoices.recalculate_totals_for_owner(db, replacement.id)
    if (
        round_money(replacement.subtotal) != current.source_subtotal
        or round_money(replacement.tax_total) != current.tax_amount
        or round_money(replacement.total) != current.replacement_total
        or round_money(replacement.balance_due) != current.replacement_total
    ):
        _error(
            "replacement_document_mismatch",
            "Invoice owner produced a replacement with different totals.",
        )

    replacement_issue = InvoiceIssuanceInput(
        issued_at=_utc(command.query.issued_at),
        due_at=_utc(command.query.due_at),
        due_date_basis=InvoiceDueDateBasis.approved_manual_override,
        due_date_basis_ref=f"historical-tax-correction:{source.id}:replacement",
        due_date_policy_version=_POLICY_VERSION,
        reason="historical_invoice_tax_correction_replacement",
    )
    Invoices.issue_draft_for_owner(
        db,
        str(replacement.id),
        issuance=replacement_issue,
        announce=False,
        apply_available_credit=False,
    )
    replacement_application = (
        AccountCreditApplications.apply_invoice_from_selected_payment_fully(
            db,
            replacement,
            payment_id=command.query.payment_id,
            expected_amount=current.replacement_total,
        )
    )
    if len(replacement_application.allocation_ids) != 1:
        _error(
            "replacement_settlement_incomplete",
            "Replacement invoice did not produce one exact payment allocation.",
        )
    replacement_allocation_id = UUID(replacement_application.allocation_ids[0])

    evidence = HistoricalInvoiceTaxCorrectionDocumentEvidence(
        account_id=command.query.account_id,
        source_invoice_id=source.id,
        source_invoice_line_id=command.query.source_invoice_line_id,
        source_invoice_closure_id=closure.id,
        source_payment_allocation_id=current.source_payment_allocation_id,
        void_evidence_invoice_id=void_evidence.id,
        subscription_invoice_id=subscription.id,
        payment_id=command.query.payment_id,
        tax_rate_id=command.query.tax_rate_id,
        subscription_payment_allocation_id=subscription_allocation_id,
        replacement_payment_allocation_id=replacement_allocation_id,
        source_subtotal=current.source_subtotal,
        subscription_total=current.subscription_total,
        tax_amount=current.tax_amount,
        replacement_total=current.replacement_total,
        currency=current.currency,
        preview_fingerprint=current.fingerprint,
        command_id=context.command_id,
        reason=reason,
    )
    Invoices.stage_historical_tax_correction_evidence_for_owner(
        db,
        replacement.id,
        evidence=evidence,
    )
    db.flush()

    payment_available_after = round_money(
        PaymentAllocations.available_amount(db, str(command.query.payment_id))
    )
    account_credit_after = round_money(
        get_spendable_account_credit_balance(
            db,
            str(command.query.account_id),
            currency=current.currency,
        )
    )
    if payment_available_after != Decimal("0.00") or account_credit_after != Decimal(
        "0.00"
    ):
        _error(
            "correction_balance_not_closed",
            "Correction did not consume the named payment and account credit exactly.",
            payment_available=str(payment_available_after),
            account_credit=str(account_credit_after),
        )

    AuditEvents.stage(
        db,
        AuditEventCreate(
            actor_type=AuditActorType.user,
            actor_id=str(command.authorized_system_user_id),
            action="correct_historical_invoice_tax",
            entity_type="invoice_tax_correction",
            entity_id=str(source.id),
            request_id=str(context.correlation_id),
            metadata_={
                "account_id": str(command.query.account_id),
                "source_invoice_id": str(source.id),
                "source_invoice_closure_id": str(closure.id),
                "source_payment_allocation_id": str(
                    current.source_payment_allocation_id
                ),
                "void_evidence_invoice_id": str(void_evidence.id),
                "subscription_invoice_id": str(subscription.id),
                "replacement_invoice_id": str(replacement.id),
                "payment_id": str(command.query.payment_id),
                "subscription_payment_allocation_id": str(subscription_allocation_id),
                "replacement_payment_allocation_id": str(replacement_allocation_id),
                "source_subtotal": str(current.source_subtotal),
                "subscription_total": str(current.subscription_total),
                "tax_amount": str(current.tax_amount),
                "replacement_total": str(current.replacement_total),
                "currency": current.currency,
                "preview_fingerprint": current.fingerprint,
                "command_id": str(context.command_id),
                "command_scope": context.scope,
                "command_reason": reason,
            },
        ),
    )
    emit_event(
        db,
        EventType.invoice_tax_correction_completed,
        {
            "source_invoice_id": str(source.id),
            "source_invoice_closure_id": str(closure.id),
            "source_payment_allocation_id": str(current.source_payment_allocation_id),
            "subscription_invoice_id": str(subscription.id),
            "replacement_invoice_id": str(replacement.id),
            "payment_id": str(command.query.payment_id),
            "source_subtotal": str(current.source_subtotal),
            "subscription_total": str(current.subscription_total),
            "tax_amount": str(current.tax_amount),
            "replacement_total": str(current.replacement_total),
            "currency": current.currency,
            "preview_fingerprint": current.fingerprint,
        },
        account_id=command.query.account_id,
        invoice_id=replacement.id,
    )
    db.flush()
    return HistoricalInvoiceTaxCorrectionResult(
        account_id=command.query.account_id,
        source_invoice_id=source.id,
        source_invoice_closure_id=closure.id,
        source_payment_allocation_id=current.source_payment_allocation_id,
        subscription_invoice_id=subscription.id,
        replacement_invoice_id=replacement.id,
        payment_id=command.query.payment_id,
        subscription_payment_allocation_id=subscription_allocation_id,
        replacement_payment_allocation_id=replacement_allocation_id,
        source_subtotal=current.source_subtotal,
        subscription_total=current.subscription_total,
        tax_amount=current.tax_amount,
        replacement_total=current.replacement_total,
        currency=current.currency,
        preview_fingerprint=current.fingerprint,
        replayed=False,
    )


@dataclass(frozen=True, slots=True)
class ExistingReplacementTaxCorrectionQuery:
    """Exact reviewed inputs for using an already-authored VAT invoice draft."""

    account_id: UUID
    source_invoice_id: UUID
    source_invoice_line_id: UUID
    replacement_invoice_id: UUID
    payment_id: UUID
    tax_rate_id: UUID
    ticket_reference: str
    approver_name: str
    currency: str = "NGN"


@dataclass(frozen=True, slots=True)
class ExistingReplacementTaxCorrectionPreview:
    disposition: HistoricalInvoiceTaxCorrectionDisposition
    reason: str
    account_id: UUID
    source_invoice_id: UUID
    source_invoice_line_id: UUID
    replacement_invoice_id: UUID
    payment_id: UUID
    payment_reference: str | None
    source_payment_allocation_id: UUID | None
    source_invoice_ledger_entry_id: UUID | None
    unallocated_credit_ledger_entry_id: UUID | None
    subtotal: Decimal
    tax_amount: Decimal
    replacement_total: Decimal
    payment_amount: Decimal
    current_account_credit: Decimal
    projected_remaining_credit: Decimal
    reconstruct_consumption_evidence: bool
    fingerprint: str

    @property
    def actionable(self) -> bool:
        return self.disposition is HistoricalInvoiceTaxCorrectionDisposition.eligible


@dataclass(frozen=True, slots=True)
class CorrectExistingReplacementTaxInvoiceCommand:
    query: ExistingReplacementTaxCorrectionQuery
    expected_preview_fingerprint: str
    permission_granted: bool
    authorized_system_user_id: UUID


@dataclass(frozen=True, slots=True)
class ExistingReplacementTaxCorrectionResult:
    account_id: UUID
    source_invoice_id: UUID
    source_invoice_closure_id: UUID
    source_payment_allocation_id: UUID
    replacement_invoice_id: UUID
    replacement_payment_allocation_id: UUID
    payment_id: UUID
    subtotal: Decimal
    tax_amount: Decimal
    replacement_total: Decimal
    remaining_credit: Decimal
    currency: str
    approval_ticket: str
    approver_name: str
    approval_recorded_at: datetime
    preview_fingerprint: str
    replayed: bool


_EXISTING_REPLACEMENT_COMMAND = OwnerCommandDefinition(
    owner=OWNER,
    concern=CONCERN,
    name="correct_historical_invoice_tax_using_existing_replacement",
)


def _existing_replacement_metadata(
    db: Session,
    query: ExistingReplacementTaxCorrectionQuery,
) -> tuple[Invoice, ExistingInvoiceTaxReplacementEvidence] | None:
    invoices = db.scalars(
        select(Invoice).where(
            Invoice.account_id == query.account_id,
            Invoice.is_active.is_(True),
        )
    ).all()
    for invoice in invoices:
        evidence = Invoices.existing_tax_replacement_evidence(invoice)
        if evidence is None or evidence.source_invoice_id != query.source_invoice_id:
            continue
        if (
            evidence.account_id != query.account_id
            or invoice.id != query.replacement_invoice_id
            or evidence.source_invoice_line_id != query.source_invoice_line_id
            or evidence.payment_id != query.payment_id
            or evidence.tax_rate_id != query.tax_rate_id
            or evidence.ticket_reference != query.ticket_reference.strip()
            or evidence.approver_name != query.approver_name.strip()
        ):
            _error(
                "existing_correction_conflict",
                "Source invoice already has different replacement-correction evidence.",
                source_invoice_id=str(query.source_invoice_id),
            )
        return invoice, evidence
    return None


def _existing_replacement_result(
    db: Session,
    *,
    invoice: Invoice,
    evidence: ExistingInvoiceTaxReplacementEvidence,
    replayed: bool,
) -> ExistingReplacementTaxCorrectionResult:
    source = db.get(Invoice, evidence.source_invoice_id)
    source_line = db.get(InvoiceLine, evidence.source_invoice_line_id)
    closure = db.get(InvoiceClosure, evidence.source_invoice_closure_id)
    source_allocation = db.get(PaymentAllocation, evidence.source_payment_allocation_id)
    replacement_allocation = db.get(
        PaymentAllocation, evidence.replacement_payment_allocation_id
    )
    replacement_lines = _active_lines(db, invoice.id, lock=False)
    payment = db.get(Payment, evidence.payment_id)
    if (
        source is None
        or source.status is not InvoiceStatus.void
        or source.account_id != evidence.account_id
        or round_money(source.total) != round_money(evidence.subtotal)
        or source_line is None
        or source_line.invoice_id != source.id
        or not source_line.is_active
        or closure is None
        or closure.invoice_id != source.id
        or closure.closure_type is not InvoiceClosureType.void
        or round_money(closure.payments_applied) != round_money(evidence.subtotal)
        or source_allocation is None
        or source_allocation.is_active
        or source_allocation.payment_id != evidence.payment_id
        or source_allocation.invoice_id != source.id
        or round_money(source_allocation.amount) != round_money(evidence.subtotal)
        or replacement_allocation is None
        or not replacement_allocation.is_active
        or replacement_allocation.payment_id != evidence.payment_id
        or replacement_allocation.invoice_id != invoice.id
        or round_money(replacement_allocation.amount)
        != round_money(evidence.replacement_total)
        or not invoice.is_active
        or invoice.status is not InvoiceStatus.paid
        or invoice.account_id != evidence.account_id
        or invoice.currency.upper() != evidence.currency
        or round_money(invoice.balance_due) != Decimal("0.00")
        or round_money(invoice.subtotal) != round_money(evidence.subtotal)
        or round_money(invoice.tax_total) != round_money(evidence.tax_amount)
        or round_money(invoice.total) != round_money(evidence.replacement_total)
        or len(replacement_lines) != 1
        or replacement_lines[0].tax_rate_id != evidence.tax_rate_id
        or replacement_lines[0].tax_application is not TaxApplication.exclusive
        or payment is None
        or not payment.is_active
        or payment.status is not PaymentStatus.succeeded
        or payment.account_id != evidence.account_id
        or payment.currency.upper() != evidence.currency
        or round_money(PaymentAllocations.available_amount(db, str(payment.id)))
        != round_money(evidence.remaining_credit)
        or round_money(
            get_spendable_account_credit_balance(
                db, str(evidence.account_id), currency=evidence.currency
            )
        )
        != round_money(evidence.remaining_credit)
    ):
        _error(
            "correction_evidence_drift",
            "Existing replacement correction evidence is incomplete or has drifted.",
            replacement_invoice_id=str(invoice.id),
        )
    return ExistingReplacementTaxCorrectionResult(
        account_id=evidence.account_id,
        source_invoice_id=evidence.source_invoice_id,
        source_invoice_closure_id=evidence.source_invoice_closure_id,
        source_payment_allocation_id=evidence.source_payment_allocation_id,
        replacement_invoice_id=invoice.id,
        replacement_payment_allocation_id=evidence.replacement_payment_allocation_id,
        payment_id=evidence.payment_id,
        subtotal=evidence.subtotal,
        tax_amount=evidence.tax_amount,
        replacement_total=evidence.replacement_total,
        remaining_credit=evidence.remaining_credit,
        currency=evidence.currency,
        approval_ticket=evidence.ticket_reference,
        approver_name=evidence.approver_name,
        approval_recorded_at=evidence.recorded_at,
        preview_fingerprint=evidence.preview_fingerprint,
        replayed=replayed,
    )


def _existing_replacement_manual_preview(
    query: ExistingReplacementTaxCorrectionQuery,
    reason: str,
    *,
    payment_reference: str | None = None,
) -> ExistingReplacementTaxCorrectionPreview:
    payload = {
        "account_id": query.account_id,
        "source_invoice_id": query.source_invoice_id,
        "source_invoice_line_id": query.source_invoice_line_id,
        "replacement_invoice_id": query.replacement_invoice_id,
        "payment_id": query.payment_id,
        "tax_rate_id": query.tax_rate_id,
        "ticket_reference": query.ticket_reference.strip(),
        "approver_name": query.approver_name.strip(),
        "reason": reason,
    }
    zero = Decimal("0.00")
    return ExistingReplacementTaxCorrectionPreview(
        disposition=HistoricalInvoiceTaxCorrectionDisposition.manual_review,
        reason=reason,
        account_id=query.account_id,
        source_invoice_id=query.source_invoice_id,
        source_invoice_line_id=query.source_invoice_line_id,
        replacement_invoice_id=query.replacement_invoice_id,
        payment_id=query.payment_id,
        payment_reference=payment_reference,
        source_payment_allocation_id=None,
        source_invoice_ledger_entry_id=None,
        unallocated_credit_ledger_entry_id=None,
        subtotal=zero,
        tax_amount=zero,
        replacement_total=zero,
        payment_amount=zero,
        current_account_credit=zero,
        projected_remaining_credit=zero,
        reconstruct_consumption_evidence=False,
        fingerprint=_fingerprint(payload),
    )


def _build_existing_replacement_preview(
    db: Session,
    query: ExistingReplacementTaxCorrectionQuery,
    *,
    lock: bool,
) -> ExistingReplacementTaxCorrectionPreview:
    currency = _normalized_currency(query.currency)
    if not query.ticket_reference.strip() or not query.approver_name.strip():
        return _existing_replacement_manual_preview(
            query, "A named Finance approver and ticket reference are required."
        )

    existing = _existing_replacement_metadata(db, query)
    if existing is not None:
        outcome = _existing_replacement_result(
            db, invoice=existing[0], evidence=existing[1], replayed=True
        )
        return ExistingReplacementTaxCorrectionPreview(
            disposition=HistoricalInvoiceTaxCorrectionDisposition.already_corrected,
            reason="The exact existing-invoice VAT correction is already complete.",
            account_id=query.account_id,
            source_invoice_id=query.source_invoice_id,
            source_invoice_line_id=query.source_invoice_line_id,
            replacement_invoice_id=query.replacement_invoice_id,
            payment_id=query.payment_id,
            payment_reference=None,
            source_payment_allocation_id=outcome.source_payment_allocation_id,
            source_invoice_ledger_entry_id=None,
            unallocated_credit_ledger_entry_id=None,
            subtotal=outcome.subtotal,
            tax_amount=outcome.tax_amount,
            replacement_total=outcome.replacement_total,
            payment_amount=round_money(
                outcome.subtotal + outcome.replacement_total + outcome.remaining_credit
            ),
            current_account_credit=outcome.remaining_credit,
            projected_remaining_credit=outcome.remaining_credit,
            reconstruct_consumption_evidence=False,
            fingerprint=outcome.preview_fingerprint,
        )
    if get_customer_vat_exemption_policy(db, account_id=query.account_id).vat_exempt:
        return _existing_replacement_manual_preview(
            query, "Customer is currently VAT-exempt and requires renewed review."
        )

    ids = sorted({query.source_invoice_id, query.replacement_invoice_id}, key=str)
    records = {
        invoice_id: (
            lock_for_update(db, Invoice, invoice_id)
            if lock
            else db.get(Invoice, invoice_id)
        )
        for invoice_id in ids
    }
    source = records[query.source_invoice_id]
    replacement = records[query.replacement_invoice_id]
    if source is None or replacement is None or source.id == replacement.id:
        return _existing_replacement_manual_preview(
            query,
            "Source and existing replacement invoices must be present and distinct.",
        )
    if any(
        invoice.account_id != query.account_id
        or invoice.currency.upper() != currency
        or not invoice.is_active
        or invoice.is_proforma
        for invoice in (source, replacement)
    ):
        return _existing_replacement_manual_preview(
            query, "Invoice account, currency, or lifecycle evidence differs."
        )
    if replacement.issued_at is None or replacement.due_at is None:
        return _existing_replacement_manual_preview(
            query, "Existing replacement draft lacks explicit issue or due dates."
        )
    issued_at = _utc(replacement.issued_at)
    due_at = _utc(replacement.due_at)
    if due_at < issued_at:
        return _existing_replacement_manual_preview(
            query, "Existing replacement due date precedes its issue date."
        )

    source_lines = _active_lines(db, source.id, lock=lock)
    replacement_lines = _active_lines(db, replacement.id, lock=lock)
    if (
        source.status is not InvoiceStatus.paid
        or round_money(source.balance_due) != Decimal("0.00")
        or round_money(source.tax_total) != Decimal("0.00")
        or len(source_lines) != 1
        or source_lines[0].id != query.source_invoice_line_id
        or source_lines[0].tax_rate_id is not None
        or round_money(source_lines[0].amount) != round_money(source.subtotal)
        or round_money(source.total) != round_money(source.subtotal)
        or replacement.status is not InvoiceStatus.draft
        or round_money(replacement.balance_due) != round_money(replacement.total)
        or len(replacement_lines) != 1
    ):
        return _existing_replacement_manual_preview(
            query, "Source or reusable replacement is not in the reviewed lifecycle."
        )

    tax_rate = (
        lock_for_update(db, TaxRate, query.tax_rate_id)
        if lock
        else db.get(TaxRate, query.tax_rate_id)
    )
    source_subtotal = round_money(source.subtotal)
    if tax_rate is None or not tax_rate.is_active or round_money(tax_rate.rate) <= 0:
        return _existing_replacement_manual_preview(
            query, "Reviewed tax rate is missing or inactive."
        )
    tax_amount = round_money(
        source_subtotal * Decimal(str(tax_rate.rate)) / Decimal("100")
    )
    replacement_total = round_money(source_subtotal + tax_amount)
    line = replacement_lines[0]
    if (
        line.tax_rate_id != tax_rate.id
        or line.tax_application is not TaxApplication.exclusive
        or round_money(line.amount) != source_subtotal
        or line.tax_rate_snapshot_version != 1
        or line.tax_rate_code_snapshot != tax_rate.code
        or line.tax_rate_percent_snapshot != tax_rate.rate
        or line.tax_rate_is_active_snapshot is not True
        or round_money(replacement.subtotal) != source_subtotal
        or round_money(replacement.tax_total) != tax_amount
        or round_money(replacement.total) != replacement_total
        or round_money(replacement.balance_due) != replacement_total
    ):
        return _existing_replacement_manual_preview(
            query,
            "Existing replacement draft does not match the reviewed VAT snapshot.",
        )

    allocations_stmt = select(PaymentAllocation).where(
        PaymentAllocation.invoice_id == source.id,
        PaymentAllocation.is_active.is_(True),
        PaymentAllocation.amount > Decimal("0.00"),
    )
    if lock:
        allocations_stmt = allocations_stmt.with_for_update()
    source_allocations = tuple(db.scalars(allocations_stmt).all())
    if (
        len(source_allocations) != 1
        or source_allocations[0].payment_id != query.payment_id
    ):
        return _existing_replacement_manual_preview(
            query, "Source invoice is not backed by one exact payment allocation."
        )
    allocation = source_allocations[0]
    if round_money(allocation.amount) != source_subtotal:
        return _existing_replacement_manual_preview(
            query, "Source payment allocation does not equal the source invoice total."
        )
    replacement_allocations = tuple(
        db.scalars(
            select(PaymentAllocation).where(
                PaymentAllocation.invoice_id == replacement.id,
                PaymentAllocation.is_active.is_(True),
            )
        ).all()
    )
    if replacement_allocations:
        return _existing_replacement_manual_preview(
            query, "Reusable replacement invoice already has payment allocations."
        )

    payment = (
        lock_for_update(db, Payment, query.payment_id)
        if lock
        else db.get(Payment, query.payment_id)
    )
    if payment is None:
        return _existing_replacement_manual_preview(
            query, "Reviewed payment was not found."
        )
    active_allocations_stmt = select(PaymentAllocation).where(
        PaymentAllocation.payment_id == payment.id,
        PaymentAllocation.is_active.is_(True),
        PaymentAllocation.amount > Decimal("0.00"),
    )
    if lock:
        active_allocations_stmt = active_allocations_stmt.with_for_update()
    active_allocations = tuple(db.scalars(active_allocations_stmt).all())
    payment_amount = round_money(payment.amount)
    unallocated_amount = round_money(payment_amount - source_subtotal)
    account_credit = round_money(
        get_spendable_account_credit_balance(
            db, str(query.account_id), currency=currency
        )
    )
    has_return = bool(
        db.scalar(
            select(PaymentRefund.id).where(PaymentRefund.payment_id == payment.id)
        )
        or db.scalar(
            select(PaymentReversal.id).where(PaymentReversal.payment_id == payment.id)
        )
    )
    if (
        not payment.is_active
        or payment.status is not PaymentStatus.succeeded
        or payment.account_id != query.account_id
        or payment.currency.upper() != currency
        or round_money(payment.refunded_amount) != Decimal("0.00")
        or has_return
        or {item.id for item in active_allocations} != {allocation.id}
        or unallocated_amount < Decimal("0.00")
        or account_credit != unallocated_amount
        or unallocated_amount + source_subtotal != payment_amount
        or round_money(account_credit + source_subtotal - replacement_total)
        != round_money(payment_amount - replacement_total)
    ):
        return _existing_replacement_manual_preview(
            query,
            "Payment is not the exact unreturned funding source for this correction.",
            payment_reference=payment.external_id,
        )

    invoice_entries_stmt = select(LedgerEntry).where(
        LedgerEntry.account_id == query.account_id,
        LedgerEntry.payment_id == payment.id,
        LedgerEntry.invoice_id == source.id,
        LedgerEntry.entry_type == LedgerEntryType.credit,
        LedgerEntry.source == LedgerSource.payment,
        LedgerEntry.currency == currency,
        LedgerEntry.amount == source_subtotal,
        LedgerEntry.is_active.is_(True),
    )
    if lock:
        invoice_entries_stmt = invoice_entries_stmt.with_for_update()
    invoice_entries = tuple(db.scalars(invoice_entries_stmt).all())
    unallocated_entries_stmt = select(LedgerEntry).where(
        LedgerEntry.account_id == query.account_id,
        LedgerEntry.payment_id == payment.id,
        LedgerEntry.invoice_id.is_(None),
        LedgerEntry.entry_type == LedgerEntryType.credit,
        LedgerEntry.source == LedgerSource.payment,
        LedgerEntry.currency == currency,
        LedgerEntry.amount == unallocated_amount,
        LedgerEntry.is_active.is_(True),
    )
    if lock:
        unallocated_entries_stmt = unallocated_entries_stmt.with_for_update()
    unallocated_entries = tuple(db.scalars(unallocated_entries_stmt).all())
    if len(invoice_entries) != 1 or len(unallocated_entries) != 1:
        return _existing_replacement_manual_preview(
            query,
            "Payment settlement ledger evidence is missing or ambiguous.",
            payment_reference=payment.external_id,
        )
    invoice_entry = invoice_entries[0]
    unallocated_entry = unallocated_entries[0]
    if allocation.ledger_entry_id not in {None, invoice_entry.id}:
        return _existing_replacement_manual_preview(
            query, "Source allocation points to different invoice ledger evidence."
        )
    settlement = db.get(PaymentSettlement, payment.id)
    if settlement is not None and (
        round_money(settlement.amount) != payment_amount
        or round_money(settlement.unallocated_amount) != unallocated_amount
        or settlement.currency.upper() != currency
        or settlement.unallocated_ledger_entry_id != unallocated_entry.id
    ):
        return _existing_replacement_manual_preview(
            query,
            "Existing payment settlement differs from exact source ledger evidence.",
        )
    if settlement is not None and allocation.ledger_entry_id != invoice_entry.id:
        return _existing_replacement_manual_preview(
            query, "Existing settlement lacks its exact allocation ledger link."
        )

    projected_credit = round_money(account_credit + source_subtotal - replacement_total)
    payload: dict[str, object] = {
        "account_id": query.account_id,
        "source_invoice_id": source.id,
        "source_invoice_updated_at": _utc(source.updated_at),
        "source_line_id": source_lines[0].id,
        "source_line_updated_at": _utc(source_lines[0].updated_at),
        "replacement_invoice_id": replacement.id,
        "replacement_invoice_updated_at": _utc(replacement.updated_at),
        "replacement_line_id": line.id,
        "replacement_line_updated_at": _utc(line.updated_at),
        "payment_id": payment.id,
        "payment_updated_at": _utc(payment.updated_at),
        "payment_reference": payment.external_id,
        "payment_amount": payment_amount,
        "allocation_id": allocation.id,
        "allocation_amount": round_money(allocation.amount),
        "invoice_ledger_entry_id": invoice_entry.id,
        "unallocated_ledger_entry_id": unallocated_entry.id,
        "consumption_entry_id": allocation.consumption_ledger_entry_id,
        "settlement_id": settlement.id if settlement is not None else None,
        "tax_rate_id": tax_rate.id,
        "tax_rate_updated_at": _utc(tax_rate.updated_at),
        "subtotal": source_subtotal,
        "tax_amount": tax_amount,
        "replacement_total": replacement_total,
        "account_credit": account_credit,
        "projected_remaining_credit": projected_credit,
        "ticket_reference": query.ticket_reference.strip(),
        "approver_name": query.approver_name.strip(),
        "issued_at": issued_at,
        "due_at": due_at,
        "currency": currency,
    }
    return ExistingReplacementTaxCorrectionPreview(
        disposition=HistoricalInvoiceTaxCorrectionDisposition.eligible,
        reason=(
            "The existing VAT draft exactly replaces the paid base-only invoice; "
            "the payment leaves the reviewed residual credit."
        ),
        account_id=query.account_id,
        source_invoice_id=source.id,
        source_invoice_line_id=source_lines[0].id,
        replacement_invoice_id=replacement.id,
        payment_id=payment.id,
        payment_reference=payment.external_id,
        source_payment_allocation_id=allocation.id,
        source_invoice_ledger_entry_id=invoice_entry.id,
        unallocated_credit_ledger_entry_id=unallocated_entry.id,
        subtotal=source_subtotal,
        tax_amount=tax_amount,
        replacement_total=replacement_total,
        payment_amount=payment_amount,
        current_account_credit=account_credit,
        projected_remaining_credit=projected_credit,
        reconstruct_consumption_evidence=allocation.consumption_ledger_entry_id is None,
        fingerprint=_fingerprint(payload),
    )


def preview_existing_replacement_tax_correction(
    db: Session,
    query: ExistingReplacementTaxCorrectionQuery,
) -> ExistingReplacementTaxCorrectionPreview:
    """Preview reuse of one existing VAT-inclusive corrective invoice draft."""

    return _build_existing_replacement_preview(db, query, lock=False)


def correct_historical_invoice_tax_using_existing_replacement(
    db: Session,
    command: CorrectExistingReplacementTaxInvoiceCommand,
    *,
    context: CommandContext,
) -> ExistingReplacementTaxCorrectionResult:
    """Atomically void the old invoice, reuse the VAT draft, and leave residual credit."""

    def operation() -> ExistingReplacementTaxCorrectionResult:
        key = (context.idempotency_key or "").strip()
        if len(key) < 16 or len(key) > 120:
            _error(
                "idempotency_key_required", "Correction idempotency key is required."
            )
        if context.scope != CORRECTION_SCOPE or not command.permission_granted:
            _error("permission_denied", "Invoice correction permission is required.")
        lock_account(db, str(command.query.account_id))
        existing = _existing_replacement_metadata(db, command.query)
        if existing is not None:
            return _existing_replacement_result(
                db, invoice=existing[0], evidence=existing[1], replayed=True
            )
        current = _build_existing_replacement_preview(db, command.query, lock=True)
        if current.fingerprint != command.expected_preview_fingerprint:
            _error(
                "stale_preview",
                "Correction evidence changed after preview; preview again.",
                current_fingerprint=current.fingerprint,
            )
        if (
            not current.actionable
            or current.source_payment_allocation_id is None
            or current.source_invoice_ledger_entry_id is None
            or current.unallocated_credit_ledger_entry_id is None
        ):
            _error(
                "not_actionable",
                current.reason,
                disposition=current.disposition.value,
            )
        source = lock_for_update(db, Invoice, current.source_invoice_id)
        replacement = lock_for_update(db, Invoice, current.replacement_invoice_id)
        payment = lock_for_update(db, Payment, current.payment_id)
        allocation = lock_for_update(
            db, PaymentAllocation, current.source_payment_allocation_id
        )
        if (
            source is None
            or replacement is None
            or payment is None
            or allocation is None
        ):
            _error(
                "invoice_missing",
                "Reviewed correction evidence disappeared under lock.",
            )
        reason = _normalized_reason(context.reason)
        if payment.settlement is None:
            try:
                Payments.reconcile_reviewed_historical_settlement_for_owner(
                    db,
                    str(payment.id),
                    PaymentSettlementReconciliationRequest(
                        allocation_ledger_entry_ids={
                            allocation.id: current.source_invoice_ledger_entry_id
                        },
                        unallocated_ledger_entry_id=current.unallocated_credit_ledger_entry_id,
                        reason=(
                            f"Ticket {command.query.ticket_reference}: reviewed VAT "
                            "correction settlement evidence"
                        ),
                    ),
                )
            except DomainError as exc:
                _error(
                    "payment_settlement_evidence_rejected",
                    "Payment owner rejected historical settlement evidence.",
                    reason=exc.code,
                )
        try:
            PaymentAllocations.stage_reviewed_legacy_consumption_evidence(
                db,
                ReviewedLegacyAllocationConsumptionEvidence(
                    account_id=command.query.account_id,
                    payment_id=payment.id,
                    invoice_id=source.id,
                    allocation_id=allocation.id,
                    invoice_ledger_entry_id=current.source_invoice_ledger_entry_id,
                    expected_amount=current.subtotal,
                    preview_fingerprint=current.fingerprint,
                    ticket_reference=command.query.ticket_reference,
                    approver_name=command.query.approver_name,
                    reason=reason,
                ),
            )
        except DomainError as exc:
            _error(
                "payment_consumption_evidence_rejected",
                "Payment owner rejected historical consumption evidence.",
                reason=exc.code,
            )

        try:
            void_preview = Invoices.preview_void_for_owner(db, source.id)
        except InvoiceOwnerError as exc:
            _error(
                "source_void_rejected",
                "Invoice owner cannot safely void the source invoice.",
                reason=exc.code,
            )
        if (
            void_preview.released_allocation_ids != (allocation.id,)
            or round_money(void_preview.payments_applied) != current.subtotal
            or round_money(void_preview.credits_applied) != Decimal("0.00")
        ):
            _error("source_void_evidence_mismatch", "Source void preview changed.")
        closure = Invoices.confirm_void_for_owner(
            db,
            source.id,
            preview_fingerprint=void_preview.fingerprint,
            idempotency_key=_child_key("void-existing-replacement", key),
            reason=reason,
            reconcile_access=False,
        ).closure
        if replacement.issued_at is None or replacement.due_at is None:
            _error(
                "replacement_document_mismatch",
                "Existing replacement draft lacks explicit issue or due dates.",
            )
        issue = InvoiceIssuanceInput(
            issued_at=_utc(replacement.issued_at),
            due_at=_utc(replacement.due_at),
            due_date_basis=InvoiceDueDateBasis.approved_manual_override,
            due_date_basis_ref=(
                f"historical-tax-correction:{source.id}:existing:{replacement.id}"
            ),
            due_date_policy_version=_POLICY_VERSION,
            reason="historical_invoice_tax_correction_existing_replacement",
        )
        Invoices.issue_draft_for_owner(
            db,
            str(replacement.id),
            issuance=issue,
            announce=False,
            apply_available_credit=False,
        )
        try:
            application = (
                AccountCreditApplications.apply_invoice_from_selected_payment_fully(
                    db,
                    replacement,
                    payment_id=payment.id,
                    expected_amount=current.replacement_total,
                )
            )
        except AccountCreditApplicationError as exc:
            _error(
                "replacement_settlement_incomplete",
                "Account-credit owner rejected the exact replacement settlement.",
                reason=exc.code,
            )
        if len(application.allocation_ids) != 1:
            _error(
                "replacement_settlement_incomplete",
                "Replacement allocation is ambiguous.",
            )
        replacement_allocation_id = UUID(application.allocation_ids[0])
        remaining = round_money(current.projected_remaining_credit)
        if (
            round_money(PaymentAllocations.available_amount(db, str(payment.id)))
            != remaining
            or round_money(
                get_spendable_account_credit_balance(
                    db, str(command.query.account_id), currency=current.currency
                )
            )
            != remaining
        ):
            _error(
                "correction_balance_mismatch",
                "Settlement did not leave the exact reviewed customer credit.",
                expected=str(remaining),
            )

        recorded_at = datetime.now(UTC)
        evidence = ExistingInvoiceTaxReplacementEvidence(
            account_id=command.query.account_id,
            source_invoice_id=source.id,
            source_invoice_line_id=command.query.source_invoice_line_id,
            source_invoice_closure_id=closure.id,
            source_payment_allocation_id=allocation.id,
            replacement_payment_allocation_id=replacement_allocation_id,
            payment_id=payment.id,
            tax_rate_id=command.query.tax_rate_id,
            subtotal=current.subtotal,
            tax_amount=current.tax_amount,
            replacement_total=current.replacement_total,
            remaining_credit=remaining,
            currency=current.currency,
            preview_fingerprint=current.fingerprint,
            command_id=context.command_id,
            ticket_reference=command.query.ticket_reference,
            approver_name=command.query.approver_name,
            recorded_at=recorded_at,
            reason=reason,
        )
        Invoices.stage_existing_tax_replacement_evidence_for_owner(
            db, replacement.id, evidence=evidence
        )
        AuditEvents.stage(
            db,
            AuditEventCreate(
                actor_type=AuditActorType.user,
                actor_id=str(command.authorized_system_user_id),
                action="correct_historical_invoice_tax_using_existing_replacement",
                entity_type="invoice_tax_correction",
                entity_id=str(source.id),
                request_id=str(context.correlation_id),
                metadata_={
                    "account_id": str(command.query.account_id),
                    "source_invoice_id": str(source.id),
                    "source_invoice_closure_id": str(closure.id),
                    "source_payment_allocation_id": str(allocation.id),
                    "replacement_invoice_id": str(replacement.id),
                    "replacement_payment_allocation_id": str(replacement_allocation_id),
                    "payment_id": str(payment.id),
                    "payment_reference": payment.external_id,
                    "subtotal": str(current.subtotal),
                    "tax_amount": str(current.tax_amount),
                    "replacement_total": str(current.replacement_total),
                    "remaining_credit": str(remaining),
                    "currency": current.currency,
                    "approver_name": command.query.approver_name.strip(),
                    "ticket_reference": command.query.ticket_reference.strip(),
                    "approval_recorded_at": recorded_at.isoformat(),
                    "evidence_fingerprint": current.fingerprint,
                    "command_id": str(context.command_id),
                    "command_reason": reason,
                },
            ),
        )
        emit_event(
            db,
            EventType.invoice_tax_correction_completed,
            {
                "source_invoice_id": str(source.id),
                "source_invoice_closure_id": str(closure.id),
                "replacement_invoice_id": str(replacement.id),
                "replacement_payment_allocation_id": str(replacement_allocation_id),
                "payment_id": str(payment.id),
                "subtotal": str(current.subtotal),
                "tax_amount": str(current.tax_amount),
                "replacement_total": str(current.replacement_total),
                "remaining_credit": str(remaining),
                "currency": current.currency,
                "preview_fingerprint": current.fingerprint,
            },
            account_id=command.query.account_id,
            invoice_id=replacement.id,
        )
        db.flush()
        return ExistingReplacementTaxCorrectionResult(
            account_id=command.query.account_id,
            source_invoice_id=source.id,
            source_invoice_closure_id=closure.id,
            source_payment_allocation_id=allocation.id,
            replacement_invoice_id=replacement.id,
            replacement_payment_allocation_id=replacement_allocation_id,
            payment_id=payment.id,
            subtotal=current.subtotal,
            tax_amount=current.tax_amount,
            replacement_total=current.replacement_total,
            remaining_credit=remaining,
            currency=current.currency,
            approval_ticket=command.query.ticket_reference.strip(),
            approver_name=command.query.approver_name.strip(),
            approval_recorded_at=recorded_at,
            preview_fingerprint=current.fingerprint,
            replayed=False,
        )

    return execute_owner_command(
        db,
        definition=_EXISTING_REPLACEMENT_COMMAND,
        context=context,
        operation=operation,
    )


__all__ = [
    "CORRECTION_SCOPE",
    "CorrectExistingReplacementTaxInvoiceCommand",
    "CorrectHistoricalInvoiceTaxCommand",
    "ExistingReplacementTaxCorrectionPreview",
    "ExistingReplacementTaxCorrectionQuery",
    "ExistingReplacementTaxCorrectionResult",
    "HistoricalInvoiceTaxCorrectionDisposition",
    "HistoricalInvoiceTaxCorrectionError",
    "HistoricalInvoiceTaxCorrectionPreview",
    "HistoricalInvoiceTaxCorrectionQuery",
    "HistoricalInvoiceTaxCorrectionResult",
    "correct_historical_invoice_tax",
    "correct_historical_invoice_tax_using_existing_replacement",
    "preview_existing_replacement_tax_correction",
    "preview_historical_invoice_tax_correction",
]
