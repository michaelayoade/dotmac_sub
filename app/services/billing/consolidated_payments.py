"""Owner for consolidated billing-account payment settlement.

This owner keeps reseller-held credit separate from subscriber ledger state.
It previews the exact FIFO or explicit invoice allocations, rechecks them while
holding the billing-account lock, and links every resulting transaction to one
``PaymentSettlement``. Routes, provider adapters, proof approval, and
reconciliation call this owner; they do not construct settled money state.
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
from uuid import UUID

from fastapi import HTTPException
from sqlalchemy import case, func
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.models.audit import AuditActorType
from app.models.billing import (
    BillingAccount,
    BillingAccountCreditAllocation,
    BillingAccountCreditAllocationItem,
    BillingAccountLedgerEntry,
    ConsolidatedPaymentReturnAllocationEvidence,
    ConsolidatedPaymentSettlementReconciliationEvidence,
    Invoice,
    InvoiceStatus,
    LedgerEntry,
    LedgerEntryType,
    LedgerSource,
    Payment,
    PaymentAllocation,
    PaymentProviderEvent,
    PaymentProviderEventFinancialEffect,
    PaymentProviderEventStatus,
    PaymentRefund,
    PaymentRefundOrigin,
    PaymentReversal,
    PaymentReversalOrigin,
    PaymentSettlement,
    PaymentSettlementOrigin,
    PaymentStatus,
    TopupIntent,
)
from app.models.idempotency import IdempotencyKey
from app.models.payment_proof import PaymentProof, PaymentProofStatus
from app.models.subscriber import Subscriber
from app.schemas.audit import AuditEventCreate
from app.schemas.billing import (
    BillingAccountCreditAllocationConfirm,
    BillingAccountCreditAllocationPreviewRead,
    BillingAccountCreditAllocationPreviewRequest,
    BillingAccountCreditAllocationResultRead,
    BillingAccountCreditInvoiceEffectRead,
    BillingAccountCreditSourceEffectRead,
    BillingAccountLedgerEvidenceCandidateRead,
    BillingAccountPaymentAllocationEffectRead,
    BillingAccountPaymentConfirm,
    BillingAccountPaymentPreviewRead,
    BillingAccountPaymentPreviewRequest,
    BillingAccountPaymentProvenanceCandidateRead,
    BillingAccountPaymentRefundPreviewRead,
    BillingAccountPaymentRefundRequest,
    BillingAccountPaymentReturnInvoiceEffectRead,
    BillingAccountPaymentReversalPreviewRead,
    BillingAccountPaymentReversalRequest,
    BillingAccountPaymentSettlementAllocationEvidenceRead,
    BillingAccountPaymentSettlementEvidenceInspectionRead,
    BillingAccountPaymentSettlementReconciliationConfirm,
    BillingAccountPaymentSettlementReconciliationPreviewRead,
    BillingAccountPaymentSettlementReconciliationRequest,
    PaymentRefundPreviewRequest,
    PaymentReversalPreviewRequest,
    PaymentSettlementEvidenceCandidateRead,
)
from app.services.audit import AuditEvents
from app.services.billing._common import (
    _assert_invoice_allocatable,
    _resolve_collection_account,
    _resolve_payment_channel,
    _validate_collection_account,
    _validate_payment_provider,
)
from app.services.billing.payments import (
    RefundCapability,
    ReversalCapability,
    _apply_payment_allocation,
    _emit_consolidated_payment_events,
    _finalize_invoice_payment_effects,
    _validate_refund_provider_event,
    _validate_reversal_provider_event,
)
from app.services.common import get_by_id, round_money, to_decimal
from app.services.events import emit_event
from app.services.events.types import EventType
from app.services.locking import lock_for_update

_IDEMPOTENCY_SCOPE = "consolidated_payment_settlement"
_RECONCILIATION_IDEMPOTENCY_SCOPE = "consolidated_settlement_reconciliation"
_CREDIT_ALLOCATION_IDEMPOTENCY_SCOPE = "consolidated_credit_allocation"
_REFUND_IDEMPOTENCY_SCOPE = "consolidated_payment_refund"
_REVERSAL_IDEMPOTENCY_SCOPE = "consolidated_payment_reversal"
_SAFE_KEY_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:/-]{15,119}")
_OPEN_INVOICE_STATUSES = (
    InvoiceStatus.issued,
    InvoiceStatus.partially_paid,
    InvoiceStatus.overdue,
)


def consolidated_settlement_key(namespace: str, source_id: str) -> str:
    """Return a stable, safe key without leaking arbitrary provider text."""
    digest = hashlib.sha256(f"{namespace}:{source_id}".encode()).hexdigest()
    return f"consolidated-{namespace}-{digest}"


@dataclass(frozen=True)
class ConsolidatedPaymentSettlementResult:
    payment: Payment
    settlement: PaymentSettlement
    preview: BillingAccountPaymentPreviewRead | None
    idempotent_replay: bool = False


@dataclass(frozen=True)
class ConsolidatedPaymentSettlementReconciliationResult:
    settlement: PaymentSettlement
    evidence: ConsolidatedPaymentSettlementReconciliationEvidence
    preview: BillingAccountPaymentSettlementReconciliationPreviewRead | None
    idempotent_replay: bool = False


@dataclass(frozen=True)
class ConsolidatedPaymentRefundResult:
    refund: PaymentRefund
    payment: Payment
    billing_account_ledger_entry: BillingAccountLedgerEntry | None
    allocation_evidence: tuple[ConsolidatedPaymentReturnAllocationEvidence, ...]
    preview: BillingAccountPaymentRefundPreviewRead | None
    idempotent_replay: bool = False

    def audit_metadata(self) -> dict[str, object]:
        return {
            "refund_id": str(self.refund.id),
            "payment_id": str(self.payment.id),
            "billing_account_id": str(self.payment.billing_account_id),
            "amount": str(self.refund.amount),
            "currency": self.refund.currency,
            "preview_fingerprint": self.refund.preview_fingerprint,
            "billing_account_ledger_entry_id": (
                str(self.billing_account_ledger_entry.id)
                if self.billing_account_ledger_entry
                else None
            ),
            "subscriber_ledger_entry_ids": [
                str(item.ledger_entry_id) for item in self.allocation_evidence
            ],
            "access_consequence": (
                self.preview.service_access_consequence
                if self.preview
                else "recheck_after_consolidated_refund"
            ),
            "idempotent_replay": self.idempotent_replay,
        }


@dataclass(frozen=True)
class ConsolidatedPaymentReversalResult:
    reversal: PaymentReversal
    payment: Payment
    billing_account_ledger_entry: BillingAccountLedgerEntry | None
    allocation_evidence: tuple[ConsolidatedPaymentReturnAllocationEvidence, ...]
    preview: BillingAccountPaymentReversalPreviewRead | None
    idempotent_replay: bool = False

    def audit_metadata(self) -> dict[str, object]:
        return {
            "reversal_id": str(self.reversal.id),
            "payment_id": str(self.payment.id),
            "billing_account_id": str(self.payment.billing_account_id),
            "amount": str(self.reversal.amount),
            "currency": self.reversal.currency,
            "preview_fingerprint": self.reversal.preview_fingerprint,
            "billing_account_ledger_entry_id": (
                str(self.billing_account_ledger_entry.id)
                if self.billing_account_ledger_entry
                else None
            ),
            "subscriber_ledger_entry_ids": [
                str(item.ledger_entry_id) for item in self.allocation_evidence
            ],
            "access_consequence": (
                self.preview.service_access_consequence
                if self.preview
                else "recheck_after_consolidated_reversal"
            ),
            "idempotent_replay": self.idempotent_replay,
        }


@dataclass(frozen=True)
class _ConsolidatedReturnPosition:
    account: BillingAccount
    payment: Payment
    gross: Decimal
    refunded_before: Decimal
    payment_net_before: Decimal
    consolidated_credit_before: Decimal
    payment_credit_available: Decimal
    active_allocations: tuple[PaymentAllocation, ...]


@dataclass(frozen=True)
class _CreditSourcePosition:
    entry: BillingAccountLedgerEntry
    payment: Payment
    available: Decimal


def _billing_account_evidenced_balance(db: Session, account: BillingAccount) -> Decimal:
    credits, debits = (
        db.query(
            func.coalesce(
                func.sum(
                    case(
                        (
                            BillingAccountLedgerEntry.entry_type
                            == LedgerEntryType.credit,
                            BillingAccountLedgerEntry.amount,
                        ),
                        else_=Decimal("0.00"),
                    )
                ),
                Decimal("0.00"),
            ),
            func.coalesce(
                func.sum(
                    case(
                        (
                            BillingAccountLedgerEntry.entry_type
                            == LedgerEntryType.debit,
                            BillingAccountLedgerEntry.amount,
                        ),
                        else_=Decimal("0.00"),
                    )
                ),
                Decimal("0.00"),
            ),
        )
        .filter(BillingAccountLedgerEntry.billing_account_id == account.id)
        .filter(BillingAccountLedgerEntry.currency == account.currency)
        .filter(BillingAccountLedgerEntry.is_active.is_(True))
        .one()
    )
    return round_money(to_decimal(credits) - to_decimal(debits))


def _assert_evidenced_projection(
    db: Session, account: BillingAccount
) -> tuple[Decimal, Decimal]:
    recorded = round_money(to_decimal(account.balance))
    evidenced = _billing_account_evidenced_balance(db, account)
    drift = round_money(recorded - evidenced)
    if drift != Decimal("0.00"):
        raise HTTPException(
            status_code=409,
            detail=(
                "Consolidated credit has historical or unbacked balance drift; "
                "reconcile exact billing-account ledger evidence before allocating"
            ),
        )
    return recorded, evidenced


def _credit_sources(
    db: Session, account: BillingAccount
) -> list[_CreditSourcePosition]:
    entries = (
        db.query(BillingAccountLedgerEntry)
        .filter(BillingAccountLedgerEntry.billing_account_id == account.id)
        .filter(BillingAccountLedgerEntry.entry_type == LedgerEntryType.credit)
        .filter(BillingAccountLedgerEntry.currency == account.currency)
        .filter(BillingAccountLedgerEntry.is_active.is_(True))
        .order_by(
            BillingAccountLedgerEntry.created_at.asc(),
            BillingAccountLedgerEntry.id.asc(),
        )
        .all()
    )
    sources: list[_CreditSourcePosition] = []
    for entry in entries:
        if entry.payment_id is None:
            raise HTTPException(
                status_code=409,
                detail="Consolidated credit source has no exact payment evidence",
            )
        payment = get_by_id(db, Payment, entry.payment_id)
        if (
            payment is None
            or not payment.is_active
            or payment.status
            not in {PaymentStatus.succeeded, PaymentStatus.partially_refunded}
            or payment.account_id is not None
            or payment.billing_account_id != account.id
            or payment.currency != account.currency
            or payment.settlement is None
            or payment.settlement.billing_account_ledger_entry_id != entry.id
            or payment.settlement.currency != account.currency
            or round_money(to_decimal(payment.settlement.unallocated_amount))
            != round_money(to_decimal(entry.amount))
        ):
            raise HTTPException(
                status_code=409,
                detail="Consolidated credit source settlement evidence is incomplete",
            )
        linked_consumption = round_money(
            to_decimal(
                db.query(
                    func.coalesce(
                        func.sum(BillingAccountCreditAllocationItem.amount),
                        Decimal("0.00"),
                    )
                )
                .filter(
                    BillingAccountCreditAllocationItem.source_billing_account_ledger_entry_id
                    == entry.id
                )
                .scalar()
            )
        )
        returned_credit = round_money(
            sum(
                (
                    round_money(to_decimal(refund.billing_account_ledger_entry.amount))
                    for refund in payment.refunds
                    if refund.billing_account_ledger_entry is not None
                ),
                Decimal("0.00"),
            )
        )
        active_allocated = round_money(
            to_decimal(
                db.query(
                    func.coalesce(func.sum(PaymentAllocation.amount), Decimal("0.00"))
                )
                .filter(PaymentAllocation.payment_id == payment.id)
                .filter(PaymentAllocation.is_active.is_(True))
                .scalar()
            )
        )
        initially_allocated = round_money(
            to_decimal(payment.settlement.amount)
            - to_decimal(payment.settlement.unallocated_amount)
            - to_decimal(payment.settlement.prepaid_amount)
        )
        later_allocated = max(
            Decimal("0.00"), round_money(active_allocated - initially_allocated)
        )
        if later_allocated != linked_consumption:
            raise HTTPException(
                status_code=409,
                detail=(
                    "Consolidated credit has historical allocation without exact "
                    "consumption evidence; reconcile it before allocating"
                ),
            )
        available = round_money(
            to_decimal(entry.amount) - linked_consumption - returned_credit
        )
        if available < Decimal("0.00"):
            raise HTTPException(
                status_code=409,
                detail="Consolidated credit consumption exceeds its source entry",
            )
        if available > Decimal("0.00"):
            sources.append(
                _CreditSourcePosition(
                    entry=entry,
                    payment=payment,
                    available=available,
                )
            )
    return sources


def _normalize_key(value: str) -> str:
    key = value.strip()
    if not _SAFE_KEY_RE.fullmatch(key):
        raise HTTPException(
            status_code=400,
            detail=(
                "Consolidated payment idempotency key must be 16-120 safe characters"
            ),
        )
    return key


def _lock_billing_account(db: Session, billing_account_id) -> BillingAccount:
    account = (
        db.query(BillingAccount)
        .filter(BillingAccount.id == billing_account_id)
        .with_for_update()
        .first()
    )
    if account is None:
        raise HTTPException(status_code=404, detail="Billing account not found")
    if not account.is_active or account.status != "active":
        raise HTTPException(status_code=409, detail="Billing account is not active")
    return account


def _validate_currency(account: BillingAccount, currency: str) -> str:
    normalized = currency.strip().upper()
    if normalized != account.currency.upper():
        raise HTTPException(
            status_code=409,
            detail="Payment currency must match the consolidated billing account",
        )
    return normalized


def _assert_billing_account_projection(db: Session, account: BillingAccount) -> Decimal:
    recorded = round_money(to_decimal(account.balance))
    evidenced = _billing_account_evidenced_balance(db, account)
    if recorded != evidenced:
        raise HTTPException(
            status_code=409,
            detail=(
                "Consolidated credit has historical or unbacked balance drift; "
                "reconcile exact billing-account ledger evidence before returning "
                "the payment"
            ),
        )
    return evidenced


def _consolidated_refund_capability(
    payment: Payment, *, origin: PaymentRefundOrigin
) -> RefundCapability:
    if not payment.is_active:
        return RefundCapability(False, "Inactive payments cannot be refunded")
    if payment.billing_account_id is None or payment.account_id is not None:
        return RefundCapability(False, "Payment is not a consolidated payment")
    if payment.settlement is None:
        return RefundCapability(False, "Consolidated settlement evidence is missing")
    if payment.status == PaymentStatus.refunded:
        return RefundCapability(False, "Payment is already fully refunded")
    if payment.status not in {
        PaymentStatus.succeeded,
        PaymentStatus.partially_refunded,
    }:
        return RefundCapability(
            False, "Only succeeded or partially refunded payments can be refunded"
        )
    if origin == PaymentRefundOrigin.manual and payment.provider_id is not None:
        return RefundCapability(
            False,
            "Provider-backed payments require a confirmed provider refund event",
        )
    return RefundCapability(True, None)


def _consolidated_reversal_capability(
    payment: Payment, *, origin: PaymentReversalOrigin
) -> ReversalCapability:
    if not payment.is_active:
        return ReversalCapability(False, "Inactive payment cannot be reversed")
    if payment.billing_account_id is None or payment.account_id is not None:
        return ReversalCapability(False, "Payment is not a consolidated payment")
    if payment.settlement is None:
        return ReversalCapability(False, "Consolidated settlement evidence is missing")
    if payment.reversal is not None or payment.status == PaymentStatus.reversed:
        return ReversalCapability(False, "Payment is already reversed")
    if payment.status not in {
        PaymentStatus.succeeded,
        PaymentStatus.partially_refunded,
    }:
        return ReversalCapability(False, "Only settled payment value can be reversed")
    if origin == PaymentReversalOrigin.manual and payment.provider_id is not None:
        return ReversalCapability(
            False,
            "Provider-backed payments require a confirmed provider reversal event",
        )
    return ReversalCapability(True, None)


def _refund_evidence_total(refund: PaymentRefund) -> Decimal:
    billing_amount = (
        round_money(to_decimal(refund.billing_account_ledger_entry.amount))
        if refund.billing_account_ledger_entry is not None
        else Decimal("0.00")
    )
    allocation_amount = round_money(
        sum(
            (
                round_money(to_decimal(item.amount))
                for item in refund.consolidated_allocation_evidence
            ),
            Decimal("0.00"),
        )
    )
    return round_money(billing_amount + allocation_amount)


def _reversal_evidence_total(reversal: PaymentReversal) -> Decimal:
    billing_amount = (
        round_money(to_decimal(reversal.billing_account_ledger_entry.amount))
        if reversal.billing_account_ledger_entry is not None
        else Decimal("0.00")
    )
    allocation_amount = round_money(
        sum(
            (
                round_money(to_decimal(item.amount))
                for item in reversal.consolidated_allocation_evidence
            ),
            Decimal("0.00"),
        )
    )
    return round_money(billing_amount + allocation_amount)


def _return_position(db: Session, payment: Payment) -> _ConsolidatedReturnPosition:
    if payment.billing_account_id is None or payment.account_id is not None:
        raise HTTPException(status_code=409, detail="Payment is not consolidated")
    account = get_by_id(db, BillingAccount, payment.billing_account_id)
    if account is None:
        raise HTTPException(status_code=409, detail="Billing account was not found")
    if payment.currency != account.currency:
        raise HTTPException(
            status_code=409, detail="Payment and billing-account currency differ"
        )
    settlement = payment.settlement
    if settlement is None or settlement.currency != payment.currency:
        raise HTTPException(
            status_code=409, detail="Consolidated settlement evidence is incomplete"
        )
    gross = round_money(to_decimal(payment.amount))
    unallocated = round_money(to_decimal(settlement.unallocated_amount))
    if gross <= Decimal("0.00") or unallocated < Decimal("0.00"):
        raise HTTPException(status_code=409, detail="Invalid consolidated settlement")
    source_entry = (
        db.get(BillingAccountLedgerEntry, settlement.billing_account_ledger_entry_id)
        if settlement.billing_account_ledger_entry_id
        else None
    )
    if unallocated > Decimal("0.00") and (
        source_entry is None
        or not source_entry.is_active
        or source_entry.billing_account_id != account.id
        or source_entry.payment_id != payment.id
        or source_entry.entry_type != LedgerEntryType.credit
        or source_entry.source != LedgerSource.payment
        or source_entry.currency != payment.currency
        or round_money(to_decimal(source_entry.amount)) != unallocated
    ):
        raise HTTPException(
            status_code=409,
            detail="Consolidated payment credit source evidence is incomplete",
        )
    if unallocated == Decimal("0.00") and source_entry is not None:
        raise HTTPException(
            status_code=409,
            detail="Zero-surplus settlement unexpectedly links credit evidence",
        )
    refunded_before = round_money(
        sum(
            (round_money(to_decimal(refund.amount)) for refund in payment.refunds),
            Decimal("0.00"),
        )
    )
    if refunded_before != round_money(to_decimal(payment.refunded_amount)):
        raise HTTPException(
            status_code=409,
            detail="Consolidated refund state conflicts with return documents",
        )
    for refund in payment.refunds:
        if _refund_evidence_total(refund) != round_money(to_decimal(refund.amount)):
            raise HTTPException(
                status_code=409,
                detail="Consolidated refund has incomplete exact ledger evidence",
            )
    active_allocations = tuple(
        sorted(
            (allocation for allocation in payment.allocations if allocation.is_active),
            key=lambda allocation: (allocation.created_at, allocation.id),
        )
    )
    allocated = round_money(
        sum(
            (round_money(to_decimal(item.amount)) for item in active_allocations),
            Decimal("0.00"),
        )
    )
    initially_allocated = round_money(
        gross - unallocated - to_decimal(settlement.prepaid_amount)
    )
    later_allocated = max(Decimal("0.00"), round_money(allocated - initially_allocated))
    returned_credit = round_money(
        sum(
            (
                round_money(to_decimal(refund.billing_account_ledger_entry.amount))
                for refund in payment.refunds
                if refund.billing_account_ledger_entry is not None
            ),
            Decimal("0.00"),
        )
    )
    payment_credit_available = round_money(
        unallocated - later_allocated - returned_credit
    )
    if payment_credit_available < Decimal("0.00"):
        raise HTTPException(
            status_code=409,
            detail="Consolidated payment return exceeds its evidenced credit source",
        )
    consolidated_credit_before = _assert_billing_account_projection(db, account)
    if payment_credit_available > consolidated_credit_before:
        raise HTTPException(
            status_code=409,
            detail="Payment credit exceeds the consolidated credit position",
        )
    payment_net_before = round_money(gross - refunded_before)
    if payment_net_before != round_money(payment_credit_available + allocated):
        raise HTTPException(
            status_code=409,
            detail=(
                "Consolidated payment value does not match exact credit and "
                "allocation evidence"
            ),
        )
    return _ConsolidatedReturnPosition(
        account=account,
        payment=payment,
        gross=gross,
        refunded_before=refunded_before,
        payment_net_before=payment_net_before,
        consolidated_credit_before=consolidated_credit_before,
        payment_credit_available=payment_credit_available,
        active_allocations=active_allocations,
    )


def _return_invoice_effects(
    db: Session,
    position: _ConsolidatedReturnPosition,
    *,
    source: LedgerSource,
) -> tuple[BillingAccountPaymentReturnInvoiceEffectRead, ...]:
    effects: list[BillingAccountPaymentReturnInvoiceEffectRead] = []
    for allocation in position.active_allocations:
        invoice = get_by_id(db, Invoice, allocation.invoice_id)
        entry = (
            db.get(LedgerEntry, allocation.ledger_entry_id)
            if allocation.ledger_entry_id
            else None
        )
        amount = round_money(to_decimal(allocation.amount))
        if (
            invoice is None
            or not invoice.is_active
            or entry is None
            or not entry.is_active
            or entry.payment_id != position.payment.id
            or entry.invoice_id != invoice.id
            or entry.account_id != invoice.account_id
            or entry.entry_type != LedgerEntryType.credit
            or entry.source != LedgerSource.payment
            or entry.currency != position.payment.currency
            or round_money(to_decimal(entry.amount)) != amount
        ):
            raise HTTPException(
                status_code=409,
                detail="Consolidated allocation ledger evidence is incomplete",
            )
        receivable_before = round_money(to_decimal(invoice.balance_due))
        effects.append(
            BillingAccountPaymentReturnInvoiceEffectRead(
                payment_allocation_id=allocation.id,
                invoice_id=invoice.id,
                account_id=invoice.account_id,
                invoice_number=invoice.invoice_number,
                receivable_before=receivable_before,
                return_amount=amount,
                receivable_after=min(
                    round_money(to_decimal(invoice.total)),
                    round_money(receivable_before + amount),
                ),
                result_ledger_entry_type=LedgerEntryType.debit,
                result_ledger_source=source,
            )
        )
    return tuple(effects)


def _return_fingerprint(kind: str, values: dict[str, object]) -> str:
    encoded = json.dumps(
        {"kind": kind, **values},
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    )
    return hashlib.sha256(encoded.encode()).hexdigest()


def _candidate_invoices(
    db: Session,
    account: BillingAccount,
    request: BillingAccountPaymentPreviewRequest,
) -> list[tuple[Invoice, Decimal]]:
    remaining = round_money(to_decimal(request.amount))
    if request.allocations:
        candidates: list[tuple[Invoice, Decimal]] = []
        seen: set = set()
        for requested in request.allocations:
            if requested.invoice_id in seen:
                raise HTTPException(
                    status_code=400,
                    detail="A consolidated payment can name each invoice only once",
                )
            seen.add(requested.invoice_id)
            invoice = get_by_id(db, Invoice, requested.invoice_id)
            if invoice is None:
                raise HTTPException(status_code=404, detail="Invoice not found")
            subscriber = get_by_id(db, Subscriber, invoice.account_id)
            if subscriber is None or subscriber.reseller_id != account.reseller_id:
                raise HTTPException(
                    status_code=400,
                    detail=(
                        "Invoice does not belong to a subscriber of this billing "
                        "account's reseller"
                    ),
                )
            if invoice.currency.upper() != request.currency.upper():
                raise HTTPException(
                    status_code=400,
                    detail="Invoice currency does not match payment currency",
                )
            _assert_invoice_allocatable(invoice)
            requested_amount = round_money(to_decimal(requested.amount))
            if requested_amount > remaining:
                raise HTTPException(
                    status_code=400,
                    detail="Allocation amount exceeds payment amount",
                )
            candidates.append((invoice, requested_amount))
            remaining = round_money(remaining - requested_amount)
        return candidates
    if not request.auto_allocate:
        return []
    return [
        (invoice, round_money(to_decimal(invoice.balance_due)))
        for invoice in (
            db.query(Invoice)
            .join(Subscriber, Invoice.account_id == Subscriber.id)
            .filter(Subscriber.reseller_id == account.reseller_id)
            .filter(Invoice.is_active.is_(True))
            .filter(Invoice.status.in_(_OPEN_INVOICE_STATUSES))
            .filter(Invoice.balance_due > Decimal("0.00"))
            .filter(Invoice.currency == request.currency)
            .order_by(Invoice.due_at.asc().nulls_last(), Invoice.created_at.asc())
            .all()
        )
    ]


def _fingerprint_payload(
    request: BillingAccountPaymentPreviewRequest,
    preview_values: dict,
) -> str:
    request_values = request.model_dump(mode="json")
    canonical = json.dumps(
        {"request": request_values, **preview_values},
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    )
    return hashlib.sha256(canonical.encode()).hexdigest()


def _proof_gross_amount(proof: PaymentProof) -> Decimal | None:
    if proof.gross_amount is not None:
        return round_money(to_decimal(proof.gross_amount))
    if proof.verified_amount is None:
        return None
    return round_money(
        to_decimal(proof.verified_amount) + to_decimal(proof.wht_amount or 0)
    )


def _reconciliation_fingerprint(
    request: BillingAccountPaymentSettlementReconciliationRequest,
    values: dict[str, object],
) -> str:
    canonical = json.dumps(
        {
            "kind": "historical_consolidated_payment_settlement_evidence",
            "request": request.model_dump(mode="json"),
            **values,
        },
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    )
    return hashlib.sha256(canonical.encode()).hexdigest()


class ConsolidatedPaymentSettlements:
    """Single writer for confirmed consolidated payment money effects."""

    @staticmethod
    def preview(
        db: Session,
        billing_account_id: str,
        request: BillingAccountPaymentPreviewRequest,
    ) -> BillingAccountPaymentPreviewRead:
        account = get_by_id(db, BillingAccount, billing_account_id)
        if account is None:
            raise HTTPException(status_code=404, detail="Billing account not found")
        if not account.is_active or account.status != "active":
            raise HTTPException(status_code=409, detail="Billing account is not active")
        consolidated_before, _evidenced = _assert_evidenced_projection(db, account)
        currency = _validate_currency(account, request.currency)
        amount = round_money(to_decimal(request.amount))
        remaining = amount
        effects: list[BillingAccountPaymentAllocationEffectRead] = []
        for invoice, requested_amount in _candidate_invoices(db, account, request):
            before = max(Decimal("0.00"), round_money(to_decimal(invoice.balance_due)))
            applied = min(remaining, requested_amount, before)
            if applied <= Decimal("0.00"):
                continue
            effects.append(
                BillingAccountPaymentAllocationEffectRead(
                    invoice_id=invoice.id,
                    account_id=invoice.account_id,
                    invoice_number=invoice.invoice_number,
                    receivable_before=before,
                    receivable_after=round_money(before - applied),
                    allocation_amount=applied,
                    ledger_entry_type=LedgerEntryType.credit,
                    ledger_source=LedgerSource.payment,
                )
            )
            remaining = round_money(remaining - applied)
            if remaining <= Decimal("0.00"):
                break
        allocated = round_money(amount - remaining)
        preview_values: dict[str, object] = {
            "billing_account_id": str(account.id),
            "payment_state": PaymentStatus.succeeded.value,
            "consolidated_credit_before": str(consolidated_before),
            "consolidated_credit_after": str(
                round_money(consolidated_before + remaining)
            ),
            "allocation_effects": [
                effect.model_dump(mode="json") for effect in effects
            ],
            "allocated_amount": str(allocated),
            "unallocated_amount": str(remaining),
            "payment_consequence": "confirmed_consolidated_payment_settlement",
            "service_access_consequence": (
                "request_reconciliation_for_paid_member_invoices_no_direct_access_decision"
            ),
        }
        return BillingAccountPaymentPreviewRead(
            billing_account_id=account.id,
            amount=amount,
            currency=currency,
            payment_state=PaymentStatus.succeeded,
            consolidated_credit_before=consolidated_before,
            consolidated_credit_after=round_money(consolidated_before + remaining),
            allocation_effects=effects,
            allocated_amount=allocated,
            unallocated_amount=remaining,
            unallocated_ledger_entry_type=(
                LedgerEntryType.credit if remaining > Decimal("0.00") else None
            ),
            unallocated_ledger_source=(
                LedgerSource.payment if remaining > Decimal("0.00") else None
            ),
            payment_consequence=str(preview_values["payment_consequence"]),
            service_access_consequence=str(
                preview_values["service_access_consequence"]
            ),
            fingerprint=_fingerprint_payload(request, preview_values),
        )

    @staticmethod
    def _replay(
        db: Session, *, key: str, fingerprint: str
    ) -> ConsolidatedPaymentSettlementResult | None:
        reservation = (
            db.query(IdempotencyKey)
            .filter(IdempotencyKey.scope == _IDEMPOTENCY_SCOPE)
            .filter(IdempotencyKey.key == key)
            .first()
        )
        if reservation is None:
            return None
        if not reservation.ref_id:
            raise HTTPException(
                status_code=409, detail="Consolidated payment is being recorded"
            )
        payment = get_by_id(db, Payment, reservation.ref_id)
        if payment is None or payment.settlement is None:
            raise HTTPException(
                status_code=409,
                detail="Consolidated payment settlement evidence is incomplete",
            )
        if payment.creation_preview_fingerprint != fingerprint:
            raise HTTPException(
                status_code=409,
                detail="Idempotency key was used with a different payment preview",
            )
        return ConsolidatedPaymentSettlementResult(
            payment=payment,
            settlement=payment.settlement,
            preview=None,
            idempotent_replay=True,
        )

    @classmethod
    def settle_verified(
        cls,
        db: Session,
        billing_account_id: str,
        request: BillingAccountPaymentPreviewRequest,
        *,
        idempotency_key: str,
        origin: PaymentSettlementOrigin,
        actor_id: str | None = None,
        commit: bool = True,
        existing_payment_id: str | None = None,
    ) -> ConsolidatedPaymentSettlementResult:
        """Confirm a provider/operator fact through the same preview contract.

        The caller's verification or approval is the confirmation boundary;
        this helper still materializes the owner preview and binds the command
        to its fingerprint before any money is written.
        """
        preview = cls.preview(db, billing_account_id, request)
        command = BillingAccountPaymentConfirm(
            **request.model_dump(),
            preview_fingerprint=preview.fingerprint,
            idempotency_key=idempotency_key,
        )
        return cls.confirm(
            db,
            billing_account_id,
            command,
            origin=origin,
            actor_id=actor_id,
            commit=commit,
            existing_payment_id=existing_payment_id,
        )

    @classmethod
    def confirm(
        cls,
        db: Session,
        billing_account_id: str,
        command: BillingAccountPaymentConfirm,
        *,
        origin: PaymentSettlementOrigin = PaymentSettlementOrigin.manual,
        actor_id: str | None = None,
        commit: bool = True,
        existing_payment_id: str | None = None,
    ) -> ConsolidatedPaymentSettlementResult:
        key = _normalize_key(command.idempotency_key)
        replay = cls._replay(db, key=key, fingerprint=command.preview_fingerprint)
        if replay is not None:
            return replay
        account = _lock_billing_account(db, billing_account_id)
        request = BillingAccountPaymentPreviewRequest(
            **command.model_dump(exclude={"preview_fingerprint", "idempotency_key"})
        )
        preview = cls.preview(db, str(account.id), request)
        if preview.fingerprint != command.preview_fingerprint:
            raise HTTPException(
                status_code=409,
                detail="Financial state changed after preview; preview again",
            )
        replay = cls._replay(db, key=key, fingerprint=command.preview_fingerprint)
        if replay is not None:
            return replay
        reservation = IdempotencyKey(scope=_IDEMPOTENCY_SCOPE, key=key)
        db.add(reservation)
        try:
            _validate_payment_provider(
                db, str(command.provider_id) if command.provider_id else None
            )
            channel = _resolve_payment_channel(
                db,
                str(command.payment_channel_id) if command.payment_channel_id else None,
                str(command.payment_method_id) if command.payment_method_id else None,
                str(command.provider_id) if command.provider_id else None,
            )
            collection_account = _resolve_collection_account(
                db,
                channel,
                preview.currency,
                str(command.collection_account_id)
                if command.collection_account_id
                else None,
            )
            if command.collection_account_id and collection_account is None:
                _validate_collection_account(
                    db, str(command.collection_account_id), preview.currency
                )
            resolved_channel_id = command.payment_channel_id or (
                channel.id if channel is not None else None
            )
            resolved_collection_account_id = command.collection_account_id or (
                collection_account.id if collection_account is not None else None
            )
            if existing_payment_id is not None:
                payment = (
                    db.query(Payment)
                    .filter(Payment.id == existing_payment_id)
                    .with_for_update()
                    .first()
                )
                if payment is None:
                    raise HTTPException(status_code=404, detail="Payment not found")
                if payment.billing_account_id != account.id or payment.account_id:
                    raise HTTPException(
                        status_code=409,
                        detail="Payment does not belong to this billing account",
                    )
                if payment.settlement is not None:
                    raise HTTPException(
                        status_code=409,
                        detail="Payment already has different settlement evidence",
                    )
                if payment.status != PaymentStatus.pending:
                    raise HTTPException(
                        status_code=409,
                        detail=(
                            "Only a no-money pending observation can be confirmed; "
                            "historical succeeded rows require reconciliation"
                        ),
                    )
                if (
                    round_money(to_decimal(payment.amount)) != preview.amount
                    or payment.currency != preview.currency
                    or payment.provider_id != command.provider_id
                    or payment.external_id != command.external_id
                ):
                    raise HTTPException(
                        status_code=409,
                        detail="Confirmed payment no longer matches its observation",
                    )
                payment.status = PaymentStatus.succeeded
                payment.paid_at = command.paid_at or datetime.now(UTC)
                payment.payment_method_id = command.payment_method_id
                payment.payment_channel_id = resolved_channel_id
                payment.collection_account_id = resolved_collection_account_id
                payment.auto_allocate_on_settlement = command.auto_allocate
                payment.creation_preview_fingerprint = preview.fingerprint
                payment.memo = command.memo
            else:
                payment = Payment(
                    billing_account_id=account.id,
                    payment_method_id=command.payment_method_id,
                    payment_channel_id=resolved_channel_id,
                    collection_account_id=resolved_collection_account_id,
                    provider_id=command.provider_id,
                    amount=preview.amount,
                    currency=preview.currency,
                    status=PaymentStatus.succeeded,
                    paid_at=command.paid_at or datetime.now(UTC),
                    auto_allocate_on_settlement=command.auto_allocate,
                    creation_preview_fingerprint=preview.fingerprint,
                    external_id=command.external_id,
                    memo=command.memo,
                )
                db.add(payment)
            db.flush()
            allocations: list[PaymentAllocation] = []
            for effect in preview.allocation_effects:
                invoice = get_by_id(db, Invoice, effect.invoice_id)
                if invoice is None:
                    raise HTTPException(status_code=404, detail="Invoice not found")
                allocation, applied = _apply_payment_allocation(
                    db, payment, invoice, effect.allocation_amount
                )
                if applied != effect.allocation_amount:
                    raise HTTPException(
                        status_code=409,
                        detail="Consolidated allocation result no longer matches preview",
                    )
                allocation.preview_fingerprint = preview.fingerprint
                allocations.append(allocation)
            billing_entry: BillingAccountLedgerEntry | None = None
            if preview.unallocated_amount > Decimal("0.00"):
                account.balance = round_money(
                    to_decimal(account.balance) + preview.unallocated_amount
                )
                account.updated_at = datetime.now(UTC)
                billing_entry = BillingAccountLedgerEntry(
                    billing_account_id=account.id,
                    payment_id=payment.id,
                    entry_type=LedgerEntryType.credit,
                    source=LedgerSource.payment,
                    amount=preview.unallocated_amount,
                    currency=preview.currency,
                    balance_after=account.balance,
                    memo="Unallocated consolidated payment credit",
                )
                db.add(billing_entry)
            db.flush()
            for allocation in allocations:
                invoice = get_by_id(db, Invoice, allocation.invoice_id)
                if invoice is not None:
                    _finalize_invoice_payment_effects(db, invoice)
            settlement = PaymentSettlement(
                payment_id=payment.id,
                billing_account_ledger_entry_id=(
                    billing_entry.id if billing_entry is not None else None
                ),
                amount=preview.amount,
                unallocated_amount=preview.unallocated_amount,
                prepaid_amount=Decimal("0.00"),
                currency=preview.currency,
                origin=origin,
                preview_fingerprint=preview.fingerprint,
                idempotency_key=key,
            )
            db.add(settlement)
            db.flush()
            AuditEvents.stage(
                db,
                AuditEventCreate(
                    actor_type=(
                        AuditActorType.user if actor_id else AuditActorType.system
                    ),
                    actor_id=actor_id,
                    action="settle_consolidated_payment",
                    entity_type="payment",
                    entity_id=str(payment.id),
                    metadata_={
                        "billing_account_id": str(account.id),
                        "settlement_id": str(settlement.id),
                        "amount": str(preview.amount),
                        "currency": preview.currency,
                        "origin": origin.value,
                        "preview_fingerprint": preview.fingerprint,
                        "allocation_ledger_entry_ids": [
                            str(allocation.ledger_entry_id)
                            for allocation in allocations
                            if allocation.ledger_entry_id is not None
                        ],
                        "billing_account_ledger_entry_id": (
                            str(billing_entry.id) if billing_entry is not None else None
                        ),
                        "allocated_amount": str(preview.allocated_amount),
                        "unallocated_amount": str(preview.unallocated_amount),
                        "consolidated_credit_before": str(
                            preview.consolidated_credit_before
                        ),
                        "consolidated_credit_after": str(
                            preview.consolidated_credit_after
                        ),
                        "payment_consequence": preview.payment_consequence,
                        "service_access_consequence": (
                            preview.service_access_consequence
                        ),
                    },
                ),
            )
            reservation.ref_id = str(payment.id)
            _emit_consolidated_payment_events(db, payment, allocations)
            db.flush()
            if commit:
                db.commit()
                db.refresh(payment)
                db.refresh(settlement)
            return ConsolidatedPaymentSettlementResult(
                payment=payment,
                settlement=settlement,
                preview=preview,
            )
        except IntegrityError as exc:
            db.rollback()
            replay = cls._replay(db, key=key, fingerprint=command.preview_fingerprint)
            if replay is not None:
                return replay
            raise HTTPException(
                status_code=409, detail="Consolidated payment is already recorded"
            ) from exc
        except Exception:
            db.rollback()
            raise

    @staticmethod
    def _provenance_candidates(
        db: Session,
        payment: Payment,
        *,
        lock: bool = False,
    ) -> list[BillingAccountPaymentProvenanceCandidateRead]:
        amount = round_money(to_decimal(payment.amount))
        candidates: list[BillingAccountPaymentProvenanceCandidateRead] = []

        event_query = (
            db.query(PaymentProviderEvent)
            .filter(PaymentProviderEvent.payment_id == payment.id)
            .order_by(
                PaymentProviderEvent.received_at.asc(), PaymentProviderEvent.id.asc()
            )
        )
        if lock:
            event_query = event_query.with_for_update()
        for event in event_query.all():
            if (
                event.status == PaymentProviderEventStatus.processed
                and event.financial_effect == PaymentProviderEventFinancialEffect.none
                and payment.provider_id is not None
                and event.provider_id == payment.provider_id
                and event.amount is not None
                and round_money(to_decimal(event.amount)) == amount
                and event.currency == payment.currency
                and not event.refunds
                and not event.reversals
            ):
                candidates.append(
                    BillingAccountPaymentProvenanceCandidateRead(
                        provenance_type="provider_event",
                        provenance_id=event.id,
                        status=event.status.value,
                        amount=amount,
                        currency=payment.currency,
                    )
                )

        proof_query = (
            db.query(PaymentProof)
            .filter(PaymentProof.payment_id == payment.id)
            .order_by(PaymentProof.created_at.asc(), PaymentProof.id.asc())
        )
        if lock:
            proof_query = proof_query.with_for_update()
        for proof in proof_query.all():
            gross = _proof_gross_amount(proof)
            if (
                proof.status == PaymentProofStatus.verified
                and proof.account_id is None
                and proof.billing_account_id == payment.billing_account_id
                and gross == amount
                and proof.currency == payment.currency
            ):
                candidates.append(
                    BillingAccountPaymentProvenanceCandidateRead(
                        provenance_type="payment_proof",
                        provenance_id=proof.id,
                        status=proof.status.value,
                        amount=amount,
                        currency=payment.currency,
                    )
                )

        topup_query = (
            db.query(TopupIntent)
            .filter(TopupIntent.completed_payment_id == payment.id)
            .order_by(TopupIntent.created_at.asc(), TopupIntent.id.asc())
        )
        if lock:
            topup_query = topup_query.with_for_update()
        for topup in topup_query.all():
            if (
                topup.status == "completed"
                and topup.account_id is None
                and topup.billing_account_id == payment.billing_account_id
                and topup.actual_amount is not None
                and round_money(to_decimal(topup.actual_amount)) == amount
                and topup.currency == payment.currency
            ):
                candidates.append(
                    BillingAccountPaymentProvenanceCandidateRead(
                        provenance_type="topup_intent",
                        provenance_id=topup.id,
                        status=topup.status,
                        amount=amount,
                        currency=payment.currency,
                    )
                )
        return candidates

    @staticmethod
    def inspect_reconciliation_evidence(
        db: Session,
        payment_id: str,
    ) -> BillingAccountPaymentSettlementEvidenceInspectionRead:
        """List exact candidates without selecting evidence or changing state."""
        payment = get_by_id(db, Payment, payment_id)
        if payment is None:
            raise HTTPException(status_code=404, detail="Payment not found")
        if payment.billing_account_id is None or payment.account_id is not None:
            raise HTTPException(status_code=409, detail="Payment is not consolidated")
        account = get_by_id(db, BillingAccount, payment.billing_account_id)
        if account is None:
            raise HTTPException(status_code=409, detail="Billing account is missing")

        allocation_entries = (
            db.query(LedgerEntry)
            .filter(LedgerEntry.payment_id == payment.id)
            .filter(LedgerEntry.entry_type == LedgerEntryType.credit)
            .filter(LedgerEntry.source == LedgerSource.payment)
            .filter(LedgerEntry.currency == payment.currency)
            .filter(LedgerEntry.is_active.is_(True))
            .order_by(LedgerEntry.created_at.asc(), LedgerEntry.id.asc())
            .all()
        )
        billing_entries = (
            db.query(BillingAccountLedgerEntry)
            .filter(BillingAccountLedgerEntry.payment_id == payment.id)
            .filter(BillingAccountLedgerEntry.billing_account_id == account.id)
            .filter(BillingAccountLedgerEntry.entry_type == LedgerEntryType.credit)
            .filter(BillingAccountLedgerEntry.source == LedgerSource.payment)
            .filter(BillingAccountLedgerEntry.currency == payment.currency)
            .filter(BillingAccountLedgerEntry.is_active.is_(True))
            .order_by(
                BillingAccountLedgerEntry.created_at.asc(),
                BillingAccountLedgerEntry.id.asc(),
            )
            .all()
        )
        recorded = round_money(to_decimal(account.balance))
        evidenced = _billing_account_evidenced_balance(db, account)
        return BillingAccountPaymentSettlementEvidenceInspectionRead(
            payment_id=payment.id,
            billing_account_id=account.id,
            payment_state=payment.status,
            payment_amount=round_money(to_decimal(payment.amount)),
            currency=payment.currency,
            already_reconciled=payment.settlement is not None,
            recorded_consolidated_credit=recorded,
            evidenced_consolidated_credit=evidenced,
            projection_drift=round_money(recorded - evidenced),
            active_allocation_ids=[
                allocation.id
                for allocation in payment.allocations
                if allocation.is_active
            ],
            allocation_candidate_entries=[
                PaymentSettlementEvidenceCandidateRead(
                    ledger_entry_id=entry.id,
                    invoice_id=entry.invoice_id,
                    entry_type=entry.entry_type,
                    source=entry.source,
                    amount=round_money(to_decimal(entry.amount)),
                    currency=entry.currency,
                )
                for entry in allocation_entries
            ],
            billing_account_candidate_entries=[
                BillingAccountLedgerEvidenceCandidateRead(
                    billing_account_ledger_entry_id=entry.id,
                    entry_type=entry.entry_type,
                    source=entry.source,
                    amount=round_money(to_decimal(entry.amount)),
                    currency=entry.currency,
                    balance_after=round_money(to_decimal(entry.balance_after)),
                )
                for entry in billing_entries
            ],
            provenance_candidates=ConsolidatedPaymentSettlements._provenance_candidates(
                db, payment
            ),
        )

    @classmethod
    def _build_reconciliation_preview(
        cls,
        db: Session,
        payment_id: str,
        request: BillingAccountPaymentSettlementReconciliationRequest,
        *,
        lock: bool = False,
    ) -> BillingAccountPaymentSettlementReconciliationPreviewRead:
        initial = get_by_id(db, Payment, payment_id)
        if initial is None:
            raise HTTPException(status_code=404, detail="Payment not found")
        if initial.billing_account_id is None or initial.account_id is not None:
            raise HTTPException(status_code=409, detail="Payment is not consolidated")

        if lock:
            account = lock_for_update(db, BillingAccount, initial.billing_account_id)
            payment = lock_for_update(db, Payment, initial.id)
        else:
            account = get_by_id(db, BillingAccount, initial.billing_account_id)
            payment = initial
        if account is None or payment is None:
            raise HTTPException(status_code=409, detail="Payment ownership is missing")
        if payment.billing_account_id != account.id or payment.account_id is not None:
            raise HTTPException(
                status_code=409,
                detail="Payment ownership changed after evidence selection",
            )
        if not payment.is_active or payment.status != PaymentStatus.succeeded:
            raise HTTPException(
                status_code=409,
                detail="Only an active historical succeeded payment can be reconciled",
            )
        if payment.settlement is not None:
            raise HTTPException(
                status_code=409, detail="Payment already has settlement evidence"
            )
        if payment.refunds or payment.reversal is not None:
            raise HTTPException(
                status_code=409,
                detail="Refunded or reversed payment evidence requires separate review",
            )

        allocation_query = (
            db.query(PaymentAllocation)
            .filter(PaymentAllocation.payment_id == payment.id)
            .order_by(PaymentAllocation.created_at.asc(), PaymentAllocation.id.asc())
        )
        if lock:
            allocation_query = allocation_query.with_for_update()
        all_allocations = allocation_query.all()
        if any(not allocation.is_active for allocation in all_allocations):
            raise HTTPException(
                status_code=409,
                detail="Inactive historical allocations require separate review",
            )
        allocations = [
            allocation for allocation in all_allocations if allocation.is_active
        ]
        expected_ids = {allocation.id for allocation in allocations}
        if set(request.allocation_ledger_entry_ids) != expected_ids:
            raise HTTPException(
                status_code=409,
                detail="Every active allocation requires one explicit ledger selection",
            )
        selected_ids = list(request.allocation_ledger_entry_ids.values())
        if len(selected_ids) != len(set(selected_ids)):
            raise HTTPException(
                status_code=409, detail="A ledger entry cannot prove two allocations"
            )

        effects: list[BillingAccountPaymentSettlementAllocationEvidenceRead] = []
        allocated = Decimal("0.00")
        for allocation in allocations:
            if allocation.consumption_ledger_entry_id is not None:
                raise HTTPException(
                    status_code=409,
                    detail="Later consolidated-credit allocations cannot be reclassified",
                )
            if (
                db.query(BillingAccountCreditAllocationItem.id)
                .filter(
                    BillingAccountCreditAllocationItem.payment_allocation_id
                    == allocation.id
                )
                .first()
                is not None
            ):
                raise HTTPException(
                    status_code=409,
                    detail="Later consolidated-credit allocations cannot be reclassified",
                )
            entry_id = request.allocation_ledger_entry_ids[allocation.id]
            entry = (
                lock_for_update(db, LedgerEntry, entry_id)
                if lock
                else get_by_id(db, LedgerEntry, entry_id)
            )
            invoice = get_by_id(db, Invoice, allocation.invoice_id)
            expected_amount = round_money(to_decimal(allocation.amount))
            if (
                entry is None
                or not entry.is_active
                or invoice is None
                or entry.payment_id != payment.id
                or entry.invoice_id != allocation.invoice_id
                or entry.account_id != invoice.account_id
                or entry.entry_type != LedgerEntryType.credit
                or entry.source != LedgerSource.payment
                or entry.currency != payment.currency
                or round_money(to_decimal(entry.amount)) != expected_amount
            ):
                raise HTTPException(
                    status_code=409,
                    detail="Selected allocation ledger entry is not an exact match",
                )
            existing_claim = (
                db.query(PaymentAllocation.id)
                .filter(PaymentAllocation.ledger_entry_id == entry.id)
                .filter(PaymentAllocation.id != allocation.id)
                .first()
            )
            if existing_claim is not None or (
                allocation.ledger_entry_id is not None
                and allocation.ledger_entry_id != entry.id
            ):
                raise HTTPException(
                    status_code=409,
                    detail="Selected allocation evidence is already claimed",
                )
            effects.append(
                BillingAccountPaymentSettlementAllocationEvidenceRead(
                    payment_allocation_id=allocation.id,
                    invoice_id=allocation.invoice_id,
                    account_id=invoice.account_id,
                    amount=expected_amount,
                    ledger_entry_id=entry.id,
                )
            )
            allocated = round_money(allocated + expected_amount)

        payment_amount = round_money(to_decimal(payment.amount))
        unallocated = round_money(payment_amount - allocated)
        if unallocated < Decimal("0.00"):
            raise HTTPException(
                status_code=409, detail="Historical allocations exceed payment amount"
            )

        billing_entry: BillingAccountLedgerEntry | None = None
        if unallocated > Decimal("0.00"):
            if request.billing_account_ledger_entry_id is None:
                raise HTTPException(
                    status_code=409,
                    detail="Unallocated remainder requires billing-account ledger evidence",
                )
            billing_entry = (
                lock_for_update(
                    db,
                    BillingAccountLedgerEntry,
                    request.billing_account_ledger_entry_id,
                )
                if lock
                else get_by_id(
                    db,
                    BillingAccountLedgerEntry,
                    request.billing_account_ledger_entry_id,
                )
            )
            if (
                billing_entry is None
                or not billing_entry.is_active
                or billing_entry.billing_account_id != account.id
                or billing_entry.payment_id != payment.id
                or billing_entry.entry_type != LedgerEntryType.credit
                or billing_entry.source != LedgerSource.payment
                or billing_entry.currency != payment.currency
                or round_money(to_decimal(billing_entry.amount)) != unallocated
            ):
                raise HTTPException(
                    status_code=409,
                    detail="Selected billing-account ledger entry is not an exact match",
                )
            existing_settlement = (
                db.query(PaymentSettlement.id)
                .filter(
                    PaymentSettlement.billing_account_ledger_entry_id
                    == billing_entry.id
                )
                .first()
            )
            if existing_settlement is not None:
                raise HTTPException(
                    status_code=409,
                    detail="Selected billing-account evidence is already claimed",
                )
        elif request.billing_account_ledger_entry_id is not None:
            raise HTTPException(
                status_code=409,
                detail="Fully allocated payment cannot select billing-account evidence",
            )

        provenance = cls._provenance_candidates(db, payment, lock=lock)
        if len(provenance) != 1:
            raise HTTPException(
                status_code=409,
                detail=(
                    "Historical consolidated payment requires exactly one matching "
                    "cash provenance record"
                ),
            )
        selected_provenance = provenance[0]
        if (
            selected_provenance.provenance_type != request.provenance_type
            or selected_provenance.provenance_id != request.provenance_id
        ):
            raise HTTPException(
                status_code=409, detail="Selected cash provenance is not an exact match"
            )
        evidence_claim = db.query(
            ConsolidatedPaymentSettlementReconciliationEvidence.id
        )
        if request.provenance_type == "provider_event":
            evidence_claim = evidence_claim.filter(
                ConsolidatedPaymentSettlementReconciliationEvidence.provider_event_id
                == request.provenance_id
            )
        elif request.provenance_type == "payment_proof":
            evidence_claim = evidence_claim.filter(
                ConsolidatedPaymentSettlementReconciliationEvidence.payment_proof_id
                == request.provenance_id
            )
        else:
            evidence_claim = evidence_claim.filter(
                ConsolidatedPaymentSettlementReconciliationEvidence.topup_intent_id
                == request.provenance_id
            )
        if evidence_claim.first() is not None:
            raise HTTPException(
                status_code=409, detail="Selected cash provenance is already claimed"
            )

        recorded = round_money(to_decimal(account.balance))
        evidenced = _billing_account_evidenced_balance(db, account)
        values: dict[str, object] = {
            "payment_id": str(payment.id),
            "billing_account_id": str(account.id),
            "payment_state": payment.status.value,
            "payment_amount": str(payment_amount),
            "currency": payment.currency,
            "recorded_consolidated_credit": str(recorded),
            "evidenced_consolidated_credit": str(evidenced),
            "projection_drift": str(round_money(recorded - evidenced)),
            "allocated_amount": str(allocated),
            "unallocated_amount": str(unallocated),
            "allocation_evidence": [
                effect.model_dump(mode="json") for effect in effects
            ],
            "billing_account_ledger_entry_id": (
                str(billing_entry.id) if billing_entry is not None else None
            ),
            "billing_account_ledger_balance_after": (
                str(round_money(to_decimal(billing_entry.balance_after)))
                if billing_entry is not None
                else None
            ),
            "provenance_type": selected_provenance.provenance_type,
            "provenance_id": str(selected_provenance.provenance_id),
            "provenance_status": selected_provenance.status,
            "money_posted": False,
            "service_access_consequence": "none_evidence_only_no_access_decision",
        }
        return BillingAccountPaymentSettlementReconciliationPreviewRead(
            payment_id=payment.id,
            billing_account_id=account.id,
            payment_state=payment.status,
            payment_amount=payment_amount,
            currency=payment.currency,
            recorded_consolidated_credit=recorded,
            evidenced_consolidated_credit=evidenced,
            projection_drift=round_money(recorded - evidenced),
            allocated_amount=allocated,
            unallocated_amount=unallocated,
            allocation_evidence=effects,
            billing_account_ledger_entry_id=(
                billing_entry.id if billing_entry is not None else None
            ),
            provenance_type=selected_provenance.provenance_type,
            provenance_id=selected_provenance.provenance_id,
            money_posted=False,
            service_access_consequence=str(values["service_access_consequence"]),
            fingerprint=_reconciliation_fingerprint(request, values),
        )

    @classmethod
    def preview_reconciliation(
        cls,
        db: Session,
        payment_id: str,
        request: BillingAccountPaymentSettlementReconciliationRequest,
    ) -> BillingAccountPaymentSettlementReconciliationPreviewRead:
        return cls._build_reconciliation_preview(db, payment_id, request)

    @staticmethod
    def _reconciliation_replay(
        db: Session,
        *,
        key: str,
        fingerprint: str,
    ) -> ConsolidatedPaymentSettlementReconciliationResult | None:
        reservation = (
            db.query(IdempotencyKey)
            .filter(IdempotencyKey.scope == _RECONCILIATION_IDEMPOTENCY_SCOPE)
            .filter(IdempotencyKey.key == key)
            .first()
        )
        if reservation is None:
            return None
        if not reservation.ref_id:
            raise HTTPException(
                status_code=409,
                detail="Consolidated settlement reconciliation is in progress",
            )
        settlement = get_by_id(db, PaymentSettlement, reservation.ref_id)
        if (
            settlement is None
            or settlement.consolidated_reconciliation_evidence is None
        ):
            raise HTTPException(
                status_code=409,
                detail="Consolidated settlement reconciliation evidence is incomplete",
            )
        if settlement.preview_fingerprint != fingerprint:
            raise HTTPException(
                status_code=409,
                detail="Idempotency key was used with a different reconciliation",
            )
        return ConsolidatedPaymentSettlementReconciliationResult(
            settlement=settlement,
            evidence=settlement.consolidated_reconciliation_evidence,
            preview=None,
            idempotent_replay=True,
        )

    @classmethod
    def reconcile_historical_evidence(
        cls,
        db: Session,
        payment_id: str,
        command: BillingAccountPaymentSettlementReconciliationConfirm,
        *,
        actor_type: AuditActorType = AuditActorType.system,
        actor_id: str | None = None,
        commit: bool = True,
    ) -> ConsolidatedPaymentSettlementReconciliationResult:
        """Confirm reviewed historical structure without posting or deciding access."""
        key = _normalize_key(command.idempotency_key)
        replay = cls._reconciliation_replay(
            db, key=key, fingerprint=command.preview_fingerprint
        )
        if replay is not None:
            return replay
        request = BillingAccountPaymentSettlementReconciliationRequest(
            **command.model_dump(exclude={"preview_fingerprint", "idempotency_key"})
        )
        preview = cls._build_reconciliation_preview(db, payment_id, request, lock=True)
        if preview.fingerprint != command.preview_fingerprint:
            raise HTTPException(
                status_code=409,
                detail="Financial evidence changed after preview; preview again",
            )
        replay = cls._reconciliation_replay(
            db, key=key, fingerprint=command.preview_fingerprint
        )
        if replay is not None:
            return replay

        reservation = IdempotencyKey(scope=_RECONCILIATION_IDEMPOTENCY_SCOPE, key=key)
        db.add(reservation)
        try:
            payment = lock_for_update(db, Payment, preview.payment_id)
            if payment is None:
                raise HTTPException(status_code=404, detail="Payment not found")
            allocations = {
                allocation.id: allocation
                for allocation in (
                    db.query(PaymentAllocation)
                    .filter(PaymentAllocation.payment_id == payment.id)
                    .with_for_update()
                    .all()
                )
            }
            for effect in preview.allocation_evidence:
                allocations[
                    effect.payment_allocation_id
                ].ledger_entry_id = effect.ledger_entry_id
            settlement = PaymentSettlement(
                payment_id=payment.id,
                billing_account_ledger_entry_id=(
                    preview.billing_account_ledger_entry_id
                ),
                amount=preview.payment_amount,
                unallocated_amount=preview.unallocated_amount,
                prepaid_amount=Decimal("0.00"),
                currency=preview.currency,
                origin=PaymentSettlementOrigin.system,
                preview_fingerprint=preview.fingerprint,
                idempotency_key=key,
            )
            db.add(settlement)
            db.flush()
            evidence_values: dict[str, object] = {
                "settlement_id": settlement.id,
                "reason": command.reason.strip(),
            }
            evidence_values[f"{command.provenance_type}_id"] = command.provenance_id
            evidence = ConsolidatedPaymentSettlementReconciliationEvidence(
                **evidence_values
            )
            db.add(evidence)
            db.flush()
            AuditEvents.stage(
                db,
                AuditEventCreate(
                    actor_type=actor_type,
                    actor_id=actor_id,
                    action="reconcile_consolidated_settlement_evidence",
                    entity_type="payment",
                    entity_id=str(payment.id),
                    metadata_={
                        "billing_account_id": str(preview.billing_account_id),
                        "settlement_id": str(settlement.id),
                        "reconciliation_evidence_id": str(evidence.id),
                        "reason": command.reason.strip(),
                        "preview_fingerprint": preview.fingerprint,
                        "allocation_ledger_entry_ids": {
                            str(effect.payment_allocation_id): str(
                                effect.ledger_entry_id
                            )
                            for effect in preview.allocation_evidence
                        },
                        "billing_account_ledger_entry_id": (
                            str(preview.billing_account_ledger_entry_id)
                            if preview.billing_account_ledger_entry_id
                            else None
                        ),
                        "provenance_type": preview.provenance_type,
                        "provenance_id": str(preview.provenance_id),
                        "payment_amount": str(preview.payment_amount),
                        "allocated_amount": str(preview.allocated_amount),
                        "unallocated_amount": str(preview.unallocated_amount),
                        "recorded_consolidated_credit": str(
                            preview.recorded_consolidated_credit
                        ),
                        "evidenced_consolidated_credit": str(
                            preview.evidenced_consolidated_credit
                        ),
                        "projection_drift": str(preview.projection_drift),
                        "money_posted": False,
                        "service_access_consequence": (
                            preview.service_access_consequence
                        ),
                    },
                ),
            )
            reservation.ref_id = str(settlement.id)
            db.flush()
            if commit:
                db.commit()
                db.refresh(settlement)
                db.refresh(evidence)
            return ConsolidatedPaymentSettlementReconciliationResult(
                settlement=settlement,
                evidence=evidence,
                preview=preview,
            )
        except IntegrityError as exc:
            db.rollback()
            replay = cls._reconciliation_replay(
                db, key=key, fingerprint=command.preview_fingerprint
            )
            if replay is not None:
                return replay
            raise HTTPException(
                status_code=409,
                detail="Consolidated settlement evidence is already reconciled",
            ) from exc
        except Exception:
            db.rollback()
            raise


def _build_consolidated_refund_preview(
    db: Session,
    payment: Payment,
    request: PaymentRefundPreviewRequest,
    *,
    origin: PaymentRefundOrigin,
    provider_event_id: UUID | None = None,
) -> BillingAccountPaymentRefundPreviewRead:
    capability = _consolidated_refund_capability(payment, origin=origin)
    if not capability.allowed:
        raise HTTPException(status_code=409, detail=capability.reason)
    event = _validate_refund_provider_event(
        db,
        payment=payment,
        origin=origin,
        provider_event_id=provider_event_id,
    )
    position = _return_position(db, payment)
    amount = (
        position.payment_net_before
        if request.amount is None
        else round_money(to_decimal(request.amount))
    )
    if amount <= Decimal("0.00"):
        raise HTTPException(status_code=400, detail="Refund amount must be positive")
    if amount > position.payment_net_before:
        raise HTTPException(
            status_code=400,
            detail=(
                "Refund amount exceeds refundable consolidated payment value "
                f"({position.payment_net_before})"
            ),
        )
    if event is not None and amount != round_money(to_decimal(event.amount)):
        raise HTTPException(
            status_code=409,
            detail="Refund amount does not match the normalized provider event",
        )
    refunded_after = round_money(position.refunded_before + amount)
    full_refund = refunded_after == position.gross
    if not full_refund and amount > position.payment_credit_available:
        raise HTTPException(
            status_code=409,
            detail=(
                "A partial consolidated refund may consume only the payment's "
                "currently evidenced unallocated credit; use a full refund to "
                "reopen member receivables"
            ),
        )
    credit_consumption = position.payment_credit_available if full_refund else amount
    invoice_effects = (
        _return_invoice_effects(db, position, source=LedgerSource.refund)
        if full_refund
        else ()
    )
    values: dict[str, object] = {
        "payment_id": str(payment.id),
        "billing_account_id": str(position.account.id),
        "origin": origin.value,
        "provider_event_id": str(provider_event_id) if provider_event_id else None,
        "reason": request.reason,
        "currency": payment.currency,
        "payment_gross": str(position.gross),
        "refunded_before": str(position.refunded_before),
        "refund_amount": str(amount),
        "refunded_after": str(refunded_after),
        "consolidated_credit_before": str(position.consolidated_credit_before),
        "consolidated_credit_consumption": str(credit_consumption),
        "invoice_effects": [
            effect.model_dump(mode="json") for effect in invoice_effects
        ],
        "service_access_consequence": (
            "recheck_member_access_after_consolidated_refund_no_direct_access_decision"
        ),
    }
    return BillingAccountPaymentRefundPreviewRead(
        payment_id=payment.id,
        billing_account_id=position.account.id,
        currency=payment.currency,
        payment_gross=position.gross,
        refunded_before=position.refunded_before,
        refundable_before=position.payment_net_before,
        refund_amount=amount,
        refunded_after=refunded_after,
        payment_net_after=round_money(position.gross - refunded_after),
        status_after=(
            PaymentStatus.refunded if full_refund else PaymentStatus.partially_refunded
        ),
        consolidated_credit_before=position.consolidated_credit_before,
        consolidated_credit_after=round_money(
            position.consolidated_credit_before - credit_consumption
        ),
        consolidated_credit_consumption=credit_consumption,
        invoice_effects=list(invoice_effects),
        billing_account_ledger_entry_type=(
            LedgerEntryType.debit if credit_consumption > Decimal("0.00") else None
        ),
        billing_account_ledger_source=(
            LedgerSource.refund if credit_consumption > Decimal("0.00") else None
        ),
        service_access_consequence=str(values["service_access_consequence"]),
        fingerprint=_return_fingerprint("consolidated_payment_refund", values),
    )


def _build_consolidated_reversal_preview(
    db: Session,
    payment: Payment,
    request: PaymentReversalPreviewRequest,
    *,
    origin: PaymentReversalOrigin,
    provider_event_id: UUID | None = None,
) -> BillingAccountPaymentReversalPreviewRead:
    capability = _consolidated_reversal_capability(payment, origin=origin)
    if not capability.allowed:
        raise HTTPException(status_code=409, detail=capability.reason)
    event = _validate_reversal_provider_event(
        db,
        payment=payment,
        origin=origin,
        provider_event_id=provider_event_id,
    )
    position = _return_position(db, payment)
    if (
        event is not None
        and round_money(to_decimal(event.amount)) != position.payment_net_before
    ):
        raise HTTPException(
            status_code=409,
            detail="Provider reversal amount does not match remaining payment value",
        )
    invoice_effects = _return_invoice_effects(db, position, source=LedgerSource.payment)
    values: dict[str, object] = {
        "payment_id": str(payment.id),
        "billing_account_id": str(position.account.id),
        "origin": origin.value,
        "provider_event_id": str(provider_event_id) if provider_event_id else None,
        "reason": request.reason.strip(),
        "currency": payment.currency,
        "payment_gross": str(position.gross),
        "refunded_before": str(position.refunded_before),
        "payment_net_before": str(position.payment_net_before),
        "consolidated_credit_before": str(position.consolidated_credit_before),
        "consolidated_credit_consumption": str(position.payment_credit_available),
        "invoice_effects": [
            effect.model_dump(mode="json") for effect in invoice_effects
        ],
        "service_access_consequence": (
            "recheck_member_access_after_consolidated_reversal_no_direct_access_decision"
        ),
    }
    return BillingAccountPaymentReversalPreviewRead(
        payment_id=payment.id,
        billing_account_id=position.account.id,
        currency=payment.currency,
        payment_gross=position.gross,
        refunded_before=position.refunded_before,
        payment_net_before=position.payment_net_before,
        reversal_amount=position.payment_net_before,
        status_after=PaymentStatus.reversed,
        consolidated_credit_before=position.consolidated_credit_before,
        consolidated_credit_after=round_money(
            position.consolidated_credit_before - position.payment_credit_available
        ),
        consolidated_credit_consumption=position.payment_credit_available,
        invoice_effects=list(invoice_effects),
        billing_account_ledger_entry_type=(
            LedgerEntryType.debit
            if position.payment_credit_available > Decimal("0.00")
            else None
        ),
        billing_account_ledger_source=(
            LedgerSource.payment
            if position.payment_credit_available > Decimal("0.00")
            else None
        ),
        service_access_consequence=str(values["service_access_consequence"]),
        fingerprint=_return_fingerprint("consolidated_payment_reversal", values),
    )


def _stage_billing_account_return(
    db: Session,
    *,
    account: BillingAccount,
    payment: Payment,
    amount: Decimal,
    source: LedgerSource,
    memo: str,
) -> BillingAccountLedgerEntry | None:
    if amount <= Decimal("0.00"):
        return None
    account.balance = round_money(to_decimal(account.balance) - amount)
    account.updated_at = datetime.now(UTC)
    entry = BillingAccountLedgerEntry(
        billing_account_id=account.id,
        payment_id=payment.id,
        entry_type=LedgerEntryType.debit,
        source=source,
        amount=amount,
        currency=payment.currency,
        balance_after=account.balance,
        memo=memo,
    )
    db.add(entry)
    db.flush()
    return entry


def _stage_allocation_return_evidence(
    db: Session,
    *,
    payment: Payment,
    effects: list[BillingAccountPaymentReturnInvoiceEffectRead],
    source: LedgerSource,
    memo: str,
    refund: PaymentRefund | None = None,
    reversal: PaymentReversal | None = None,
) -> tuple[ConsolidatedPaymentReturnAllocationEvidence, ...]:
    evidence: list[ConsolidatedPaymentReturnAllocationEvidence] = []
    touched: set[UUID] = set()
    for effect in effects:
        allocation = lock_for_update(
            db, PaymentAllocation, effect.payment_allocation_id
        )
        invoice = lock_for_update(db, Invoice, effect.invoice_id)
        if (
            allocation is None
            or not allocation.is_active
            or allocation.payment_id != payment.id
            or allocation.invoice_id != effect.invoice_id
            or invoice is None
            or invoice.account_id != effect.account_id
            or round_money(to_decimal(allocation.amount)) != effect.return_amount
        ):
            raise HTTPException(
                status_code=409,
                detail="Consolidated allocation changed after return preview",
            )
        result_entry = LedgerEntry(
            account_id=invoice.account_id,
            invoice_id=invoice.id,
            payment_id=payment.id,
            entry_type=LedgerEntryType.debit,
            source=source,
            amount=effect.return_amount,
            currency=payment.currency,
            memo=memo,
        )
        db.add(result_entry)
        db.flush()
        item = ConsolidatedPaymentReturnAllocationEvidence(
            refund_id=refund.id if refund else None,
            reversal_id=reversal.id if reversal else None,
            payment_allocation_id=allocation.id,
            ledger_entry_id=result_entry.id,
            amount=effect.return_amount,
        )
        db.add(item)
        allocation.is_active = False
        evidence.append(item)
        touched.add(invoice.id)
    db.flush()
    for invoice_id in touched:
        invoice = get_by_id(db, Invoice, invoice_id)
        if invoice is not None:
            _finalize_invoice_payment_effects(db, invoice)
    return tuple(evidence)


def _emit_consolidated_return_events(
    db: Session,
    *,
    event_type: EventType,
    payment: Payment,
    return_id: UUID,
    amount: Decimal,
    effects: list[BillingAccountPaymentReturnInvoiceEffectRead],
    ledger_entry_ids: list[UUID],
) -> None:
    by_account: dict[UUID, list[UUID]] = {}
    for effect in effects:
        by_account.setdefault(effect.account_id, []).append(effect.invoice_id)
    for account_id, invoice_ids in by_account.items():
        emit_event(
            db,
            event_type,
            {
                "payment_id": str(payment.id),
                "return_id": str(return_id),
                "billing_account_id": str(payment.billing_account_id),
                "amount": str(amount),
                "currency": payment.currency,
                "invoice_ids": [str(invoice_id) for invoice_id in invoice_ids],
                "ledger_entry_ids": [str(entry_id) for entry_id in ledger_entry_ids],
                "service_access_consequence": ("request_member_account_reconciliation"),
            },
            account_id=account_id,
        )


class ConsolidatedPaymentRefunds:
    @staticmethod
    def capability(db: Session, payment_id: str) -> RefundCapability:
        payment = get_by_id(db, Payment, payment_id)
        if payment is None:
            raise HTTPException(status_code=404, detail="Payment not found")
        return _consolidated_refund_capability(
            payment, origin=PaymentRefundOrigin.manual
        )

    @staticmethod
    def preview(
        db: Session,
        payment_id: str,
        request: PaymentRefundPreviewRequest,
    ) -> BillingAccountPaymentRefundPreviewRead:
        payment = get_by_id(db, Payment, payment_id)
        if payment is None:
            raise HTTPException(status_code=404, detail="Payment not found")
        return _build_consolidated_refund_preview(
            db, payment, request, origin=PaymentRefundOrigin.manual
        )

    @staticmethod
    def _replay(
        db: Session, *, key: str, payment_id: str, fingerprint: str
    ) -> ConsolidatedPaymentRefundResult | None:
        reservation = (
            db.query(IdempotencyKey)
            .filter(IdempotencyKey.scope == _REFUND_IDEMPOTENCY_SCOPE)
            .filter(IdempotencyKey.key == key)
            .first()
        )
        if reservation is None:
            return None
        if not reservation.ref_id:
            raise HTTPException(status_code=409, detail="Refund is being processed")
        refund = get_by_id(db, PaymentRefund, reservation.ref_id)
        if (
            refund is None
            or str(refund.payment_id) != payment_id
            or refund.preview_fingerprint != fingerprint
        ):
            raise HTTPException(
                status_code=409, detail="Refund replay evidence does not match"
            )
        payment = get_by_id(db, Payment, refund.payment_id)
        if payment is None or _refund_evidence_total(refund) != round_money(
            to_decimal(refund.amount)
        ):
            raise HTTPException(status_code=409, detail="Refund evidence is incomplete")
        return ConsolidatedPaymentRefundResult(
            refund=refund,
            payment=payment,
            billing_account_ledger_entry=refund.billing_account_ledger_entry,
            allocation_evidence=tuple(refund.consolidated_allocation_evidence),
            preview=None,
            idempotent_replay=True,
        )

    @classmethod
    def confirm(
        cls,
        db: Session,
        payment_id: str,
        command: BillingAccountPaymentRefundRequest,
        *,
        origin: PaymentRefundOrigin = PaymentRefundOrigin.manual,
        provider_event_id: UUID | None = None,
        actor_id: str | None = None,
        commit: bool = True,
    ) -> ConsolidatedPaymentRefundResult:
        key = _normalize_key(command.idempotency_key)
        replay = cls._replay(
            db, key=key, payment_id=payment_id, fingerprint=command.preview_fingerprint
        )
        if replay is not None:
            return replay
        initial = get_by_id(db, Payment, payment_id)
        if initial is None or initial.billing_account_id is None:
            raise HTTPException(
                status_code=404, detail="Consolidated payment not found"
            )
        account = _lock_billing_account(db, initial.billing_account_id)
        payment = lock_for_update(db, Payment, initial.id)
        if payment is None:
            raise HTTPException(status_code=404, detail="Payment not found")
        db.query(BillingAccountLedgerEntry).filter(
            BillingAccountLedgerEntry.billing_account_id == account.id
        ).with_for_update().all()
        db.query(PaymentAllocation).filter(
            PaymentAllocation.payment_id == payment.id
        ).with_for_update().all()
        preview = _build_consolidated_refund_preview(
            db,
            payment,
            PaymentRefundPreviewRequest(amount=command.amount, reason=command.reason),
            origin=origin,
            provider_event_id=provider_event_id,
        )
        if preview.fingerprint != command.preview_fingerprint:
            raise HTTPException(
                status_code=409,
                detail="Financial state changed after preview; preview again",
            )
        replay = cls._replay(
            db, key=key, payment_id=payment_id, fingerprint=command.preview_fingerprint
        )
        if replay is not None:
            return replay
        reservation = IdempotencyKey(scope=_REFUND_IDEMPOTENCY_SCOPE, key=key)
        db.add(reservation)
        try:
            billing_entry = _stage_billing_account_return(
                db,
                account=account,
                payment=payment,
                amount=preview.consolidated_credit_consumption,
                source=LedgerSource.refund,
                memo=command.reason or f"Refund consolidated payment {payment.id}",
            )
            refund = PaymentRefund(
                payment_id=payment.id,
                provider_event_id=provider_event_id,
                ledger_entry_id=None,
                billing_account_ledger_entry_id=(
                    billing_entry.id if billing_entry else None
                ),
                credit_consumption_ledger_entry_id=None,
                amount=preview.refund_amount,
                currency=preview.currency,
                origin=origin,
                reason=command.reason,
                preview_fingerprint=preview.fingerprint,
            )
            db.add(refund)
            db.flush()
            evidence = _stage_allocation_return_evidence(
                db,
                payment=payment,
                effects=preview.invoice_effects,
                source=LedgerSource.refund,
                memo=f"Consolidated payment refund {refund.id}",
                refund=refund,
            )
            payment.refunded_amount = preview.refunded_after
            payment.status = preview.status_after
            reservation.ref_id = str(refund.id)
            AuditEvents.stage(
                db,
                AuditEventCreate(
                    actor_type=(
                        AuditActorType.user if actor_id else AuditActorType.system
                    ),
                    actor_id=actor_id,
                    action="refund_consolidated_payment",
                    entity_type="payment",
                    entity_id=str(payment.id),
                    metadata_={
                        "refund_id": str(refund.id),
                        "billing_account_id": str(account.id),
                        "amount": str(refund.amount),
                        "currency": refund.currency,
                        "origin": origin.value,
                        "provider_event_id": (
                            str(provider_event_id) if provider_event_id else None
                        ),
                        "preview_fingerprint": preview.fingerprint,
                        "billing_account_ledger_entry_id": (
                            str(billing_entry.id) if billing_entry else None
                        ),
                        "subscriber_ledger_entry_ids": [
                            str(item.ledger_entry_id) for item in evidence
                        ],
                        "consolidated_credit_before": str(
                            preview.consolidated_credit_before
                        ),
                        "consolidated_credit_after": str(
                            preview.consolidated_credit_after
                        ),
                        "service_access_consequence": (
                            preview.service_access_consequence
                        ),
                    },
                ),
            )
            _emit_consolidated_return_events(
                db,
                event_type=EventType.payment_refunded,
                payment=payment,
                return_id=refund.id,
                amount=refund.amount,
                effects=preview.invoice_effects,
                ledger_entry_ids=[item.ledger_entry_id for item in evidence],
            )
            db.flush()
            if commit:
                db.commit()
                db.refresh(payment)
                db.refresh(refund)
                if billing_entry:
                    db.refresh(billing_entry)
            return ConsolidatedPaymentRefundResult(
                refund=refund,
                payment=payment,
                billing_account_ledger_entry=billing_entry,
                allocation_evidence=evidence,
                preview=preview,
            )
        except IntegrityError as exc:
            db.rollback()
            replay = cls._replay(
                db,
                key=key,
                payment_id=payment_id,
                fingerprint=command.preview_fingerprint,
            )
            if replay is not None:
                return replay
            raise HTTPException(
                status_code=409, detail="Refund is already being processed"
            ) from exc
        except Exception:
            db.rollback()
            raise

    @classmethod
    def process_provider_event(
        cls,
        db: Session,
        *,
        payment_id: str,
        provider_event_id: UUID,
        commit: bool = False,
    ) -> ConsolidatedPaymentRefundResult:
        payment = get_by_id(db, Payment, payment_id)
        if payment is None:
            raise HTTPException(status_code=404, detail="Payment not found")
        event = _validate_refund_provider_event(
            db,
            payment=payment,
            origin=PaymentRefundOrigin.provider_event,
            provider_event_id=provider_event_id,
        )
        assert event is not None
        request = PaymentRefundPreviewRequest(
            amount=event.amount,
            reason=f"Confirmed provider refund event {provider_event_id}",
        )
        preview = _build_consolidated_refund_preview(
            db,
            payment,
            request,
            origin=PaymentRefundOrigin.provider_event,
            provider_event_id=provider_event_id,
        )
        return cls.confirm(
            db,
            payment_id,
            BillingAccountPaymentRefundRequest(
                **request.model_dump(),
                preview_fingerprint=preview.fingerprint,
                idempotency_key=f"provider-refund-{provider_event_id}",
            ),
            origin=PaymentRefundOrigin.provider_event,
            provider_event_id=provider_event_id,
            commit=commit,
        )


class ConsolidatedPaymentReversals:
    @staticmethod
    def capability(db: Session, payment_id: str) -> ReversalCapability:
        payment = get_by_id(db, Payment, payment_id)
        if payment is None:
            raise HTTPException(status_code=404, detail="Payment not found")
        return _consolidated_reversal_capability(
            payment, origin=PaymentReversalOrigin.manual
        )

    @staticmethod
    def preview(
        db: Session,
        payment_id: str,
        request: PaymentReversalPreviewRequest,
    ) -> BillingAccountPaymentReversalPreviewRead:
        payment = get_by_id(db, Payment, payment_id)
        if payment is None:
            raise HTTPException(status_code=404, detail="Payment not found")
        return _build_consolidated_reversal_preview(
            db, payment, request, origin=PaymentReversalOrigin.manual
        )

    @staticmethod
    def _replay(
        db: Session, *, key: str, payment_id: str, fingerprint: str
    ) -> ConsolidatedPaymentReversalResult | None:
        reservation = (
            db.query(IdempotencyKey)
            .filter(IdempotencyKey.scope == _REVERSAL_IDEMPOTENCY_SCOPE)
            .filter(IdempotencyKey.key == key)
            .first()
        )
        if reservation is None:
            return None
        if not reservation.ref_id:
            raise HTTPException(
                status_code=409, detail="Payment reversal is being processed"
            )
        reversal = get_by_id(db, PaymentReversal, reservation.ref_id)
        if (
            reversal is None
            or str(reversal.payment_id) != payment_id
            or reversal.preview_fingerprint != fingerprint
        ):
            raise HTTPException(
                status_code=409, detail="Reversal replay evidence does not match"
            )
        payment = get_by_id(db, Payment, reversal.payment_id)
        if payment is None or _reversal_evidence_total(reversal) != round_money(
            to_decimal(reversal.amount)
        ):
            raise HTTPException(
                status_code=409, detail="Reversal evidence is incomplete"
            )
        return ConsolidatedPaymentReversalResult(
            reversal=reversal,
            payment=payment,
            billing_account_ledger_entry=reversal.billing_account_ledger_entry,
            allocation_evidence=tuple(reversal.consolidated_allocation_evidence),
            preview=None,
            idempotent_replay=True,
        )

    @classmethod
    def confirm(
        cls,
        db: Session,
        payment_id: str,
        command: BillingAccountPaymentReversalRequest,
        *,
        origin: PaymentReversalOrigin = PaymentReversalOrigin.manual,
        provider_event_id: UUID | None = None,
        actor_id: str | None = None,
        commit: bool = True,
    ) -> ConsolidatedPaymentReversalResult:
        key = _normalize_key(command.idempotency_key)
        replay = cls._replay(
            db, key=key, payment_id=payment_id, fingerprint=command.preview_fingerprint
        )
        if replay is not None:
            return replay
        initial = get_by_id(db, Payment, payment_id)
        if initial is None or initial.billing_account_id is None:
            raise HTTPException(
                status_code=404, detail="Consolidated payment not found"
            )
        account = _lock_billing_account(db, initial.billing_account_id)
        payment = lock_for_update(db, Payment, initial.id)
        if payment is None:
            raise HTTPException(status_code=404, detail="Payment not found")
        db.query(BillingAccountLedgerEntry).filter(
            BillingAccountLedgerEntry.billing_account_id == account.id
        ).with_for_update().all()
        db.query(PaymentAllocation).filter(
            PaymentAllocation.payment_id == payment.id
        ).with_for_update().all()
        preview = _build_consolidated_reversal_preview(
            db,
            payment,
            PaymentReversalPreviewRequest(reason=command.reason),
            origin=origin,
            provider_event_id=provider_event_id,
        )
        if preview.fingerprint != command.preview_fingerprint:
            raise HTTPException(
                status_code=409,
                detail="Financial state changed after preview; preview again",
            )
        replay = cls._replay(
            db, key=key, payment_id=payment_id, fingerprint=command.preview_fingerprint
        )
        if replay is not None:
            return replay
        reservation = IdempotencyKey(scope=_REVERSAL_IDEMPOTENCY_SCOPE, key=key)
        db.add(reservation)
        try:
            billing_entry = _stage_billing_account_return(
                db,
                account=account,
                payment=payment,
                amount=preview.consolidated_credit_consumption,
                source=LedgerSource.payment,
                memo=f"Consolidated payment reversal: {command.reason.strip()}",
            )
            reversal = PaymentReversal(
                payment_id=payment.id,
                provider_event_id=provider_event_id,
                ledger_entry_id=None,
                billing_account_ledger_entry_id=(
                    billing_entry.id if billing_entry else None
                ),
                credit_consumption_ledger_entry_id=None,
                amount=preview.reversal_amount,
                currency=preview.currency,
                origin=origin,
                reason=command.reason.strip(),
                preview_fingerprint=preview.fingerprint,
            )
            db.add(reversal)
            db.flush()
            evidence = _stage_allocation_return_evidence(
                db,
                payment=payment,
                effects=preview.invoice_effects,
                source=LedgerSource.payment,
                memo=f"Consolidated payment reversal {reversal.id}",
                reversal=reversal,
            )
            payment.status = PaymentStatus.reversed
            reservation.ref_id = str(reversal.id)
            AuditEvents.stage(
                db,
                AuditEventCreate(
                    actor_type=(
                        AuditActorType.user if actor_id else AuditActorType.system
                    ),
                    actor_id=actor_id,
                    action="reverse_consolidated_payment",
                    entity_type="payment",
                    entity_id=str(payment.id),
                    metadata_={
                        "reversal_id": str(reversal.id),
                        "billing_account_id": str(account.id),
                        "amount": str(reversal.amount),
                        "currency": reversal.currency,
                        "origin": origin.value,
                        "provider_event_id": (
                            str(provider_event_id) if provider_event_id else None
                        ),
                        "preview_fingerprint": preview.fingerprint,
                        "billing_account_ledger_entry_id": (
                            str(billing_entry.id) if billing_entry else None
                        ),
                        "subscriber_ledger_entry_ids": [
                            str(item.ledger_entry_id) for item in evidence
                        ],
                        "consolidated_credit_before": str(
                            preview.consolidated_credit_before
                        ),
                        "consolidated_credit_after": str(
                            preview.consolidated_credit_after
                        ),
                        "service_access_consequence": (
                            preview.service_access_consequence
                        ),
                    },
                ),
            )
            _emit_consolidated_return_events(
                db,
                event_type=EventType.payment_reversed,
                payment=payment,
                return_id=reversal.id,
                amount=reversal.amount,
                effects=preview.invoice_effects,
                ledger_entry_ids=[item.ledger_entry_id for item in evidence],
            )
            db.flush()
            if commit:
                db.commit()
                db.refresh(payment)
                db.refresh(reversal)
                if billing_entry:
                    db.refresh(billing_entry)
            return ConsolidatedPaymentReversalResult(
                reversal=reversal,
                payment=payment,
                billing_account_ledger_entry=billing_entry,
                allocation_evidence=evidence,
                preview=preview,
            )
        except IntegrityError as exc:
            db.rollback()
            replay = cls._replay(
                db,
                key=key,
                payment_id=payment_id,
                fingerprint=command.preview_fingerprint,
            )
            if replay is not None:
                return replay
            raise HTTPException(
                status_code=409, detail="Payment reversal is already being processed"
            ) from exc
        except Exception:
            db.rollback()
            raise

    @classmethod
    def process_provider_event(
        cls,
        db: Session,
        *,
        payment_id: str,
        provider_event_id: UUID,
        commit: bool = False,
    ) -> ConsolidatedPaymentReversalResult:
        payment = get_by_id(db, Payment, payment_id)
        if payment is None:
            raise HTTPException(status_code=404, detail="Payment not found")
        event = _validate_reversal_provider_event(
            db,
            payment=payment,
            origin=PaymentReversalOrigin.provider_event,
            provider_event_id=provider_event_id,
        )
        assert event is not None
        request = PaymentReversalPreviewRequest(
            reason=f"Confirmed provider reversal event {provider_event_id}"
        )
        preview = _build_consolidated_reversal_preview(
            db,
            payment,
            request,
            origin=PaymentReversalOrigin.provider_event,
            provider_event_id=provider_event_id,
        )
        return cls.confirm(
            db,
            payment_id,
            BillingAccountPaymentReversalRequest(
                **request.model_dump(),
                preview_fingerprint=preview.fingerprint,
                idempotency_key=f"provider-reversal-{provider_event_id}",
            ),
            origin=PaymentReversalOrigin.provider_event,
            provider_event_id=provider_event_id,
            commit=commit,
        )


class ConsolidatedCreditAllocations:
    """Owner for moving evidenced reseller credit to subscriber receivables."""

    @staticmethod
    def capability(
        db: Session, billing_account_id: str, subscriber_id: str
    ) -> dict[str, object]:
        try:
            preview = ConsolidatedCreditAllocations.preview(
                db,
                billing_account_id,
                subscriber_id,
                BillingAccountCreditAllocationPreviewRequest(),
            )
        except HTTPException as exc:
            detail = (
                exc.detail if isinstance(exc.detail, str) else "Allocation unavailable"
            )
            return {
                "allowed": False,
                "maximum": Decimal("0.00"),
                "reason": detail,
            }
        return {
            "allowed": True,
            "maximum": preview.allocation_amount,
            "reason": None,
        }

    @staticmethod
    def preview(
        db: Session,
        billing_account_id: str,
        subscriber_id: str,
        request: BillingAccountCreditAllocationPreviewRequest,
    ) -> BillingAccountCreditAllocationPreviewRead:
        account = get_by_id(db, BillingAccount, billing_account_id)
        if account is None:
            raise HTTPException(status_code=404, detail="Billing account not found")
        if not account.is_active or account.status != "active":
            raise HTTPException(status_code=409, detail="Billing account is not active")
        subscriber = get_by_id(db, Subscriber, subscriber_id)
        if subscriber is None or subscriber.reseller_id != account.reseller_id:
            raise HTTPException(status_code=404, detail="Subscriber not found")
        recorded, evidenced = _assert_evidenced_projection(db, account)
        sources = _credit_sources(db, account)
        available_credit = round_money(
            sum((source.available for source in sources), Decimal("0.00"))
        )
        if available_credit != evidenced:
            raise HTTPException(
                status_code=409,
                detail=(
                    "Consolidated credit has a historical debit without exact "
                    "source-consumption evidence; reconcile it before allocating"
                ),
            )
        if available_credit <= Decimal("0.00"):
            raise HTTPException(
                status_code=409, detail="No evidenced consolidated credit is available"
            )
        invoices = (
            db.query(Invoice)
            .filter(Invoice.account_id == subscriber.id)
            .filter(Invoice.is_active.is_(True))
            .filter(Invoice.status.in_(_OPEN_INVOICE_STATUSES))
            .filter(Invoice.balance_due > Decimal("0.00"))
            .filter(Invoice.currency == account.currency)
            .order_by(
                Invoice.due_at.asc().nulls_last(),
                Invoice.created_at.asc(),
                Invoice.id.asc(),
            )
            .all()
        )
        receivable_before = round_money(
            sum(
                (round_money(to_decimal(invoice.balance_due)) for invoice in invoices),
                Decimal("0.00"),
            )
        )
        if receivable_before <= Decimal("0.00"):
            raise HTTPException(
                status_code=409, detail="Subscriber has no eligible open receivable"
            )
        maximum = min(available_credit, receivable_before)
        amount = (
            maximum
            if request.amount is None
            else round_money(to_decimal(request.amount))
        )
        if amount <= Decimal("0.00"):
            raise HTTPException(
                status_code=400, detail="Allocation amount must be positive"
            )
        if amount > maximum:
            raise HTTPException(
                status_code=409,
                detail=f"Allocation exceeds the owner-confirmed maximum ({maximum})",
            )

        remaining = amount
        source_remaining = {source.entry.id: source.available for source in sources}
        source_consumed = {source.entry.id: Decimal("0.00") for source in sources}
        invoice_effects: list[BillingAccountCreditInvoiceEffectRead] = []
        for invoice in invoices:
            invoice_remaining = round_money(to_decimal(invoice.balance_due))
            invoice_before = invoice_remaining
            for source in sources:
                if remaining <= Decimal("0.00") or invoice_remaining <= Decimal("0.00"):
                    break
                available = source_remaining[source.entry.id]
                if available <= Decimal("0.00"):
                    continue
                applied = min(remaining, invoice_remaining, available)
                invoice_effects.append(
                    BillingAccountCreditInvoiceEffectRead(
                        invoice_id=invoice.id,
                        invoice_number=invoice.invoice_number,
                        receivable_before=invoice_before,
                        allocation_amount=applied,
                        receivable_after=round_money(invoice_remaining - applied),
                        source_billing_account_ledger_entry_id=source.entry.id,
                        source_payment_id=source.payment.id,
                    )
                )
                source_remaining[source.entry.id] = round_money(available - applied)
                source_consumed[source.entry.id] = round_money(
                    source_consumed[source.entry.id] + applied
                )
                invoice_remaining = round_money(invoice_remaining - applied)
                remaining = round_money(remaining - applied)
            if remaining <= Decimal("0.00"):
                break
        if remaining != Decimal("0.00"):
            raise HTTPException(
                status_code=409,
                detail="Exact consolidated credit sources cannot satisfy the allocation",
            )
        source_effects = [
            BillingAccountCreditSourceEffectRead(
                billing_account_ledger_entry_id=source.entry.id,
                payment_id=source.payment.id,
                available_before=source.available,
                consumed_amount=source_consumed[source.entry.id],
                available_after=source_remaining[source.entry.id],
            )
            for source in sources
            if source_consumed[source.entry.id] > Decimal("0.00")
        ]
        fingerprint_payload = {
            "kind": "consolidated_credit_allocation",
            "billing_account_id": str(account.id),
            "subscriber_id": str(subscriber.id),
            "recorded_consolidated_credit": str(recorded),
            "evidenced_consolidated_credit": str(evidenced),
            "subscriber_receivable_before": str(receivable_before),
            "allocation_amount": str(amount),
            "source_effects": [
                effect.model_dump(mode="json") for effect in source_effects
            ],
            "invoice_effects": [
                effect.model_dump(mode="json") for effect in invoice_effects
            ],
            "service_access_consequence": (
                "request_reconciliation_for_paid_member_invoices_no_direct_access_decision"
            ),
        }
        fingerprint = hashlib.sha256(
            json.dumps(
                fingerprint_payload,
                sort_keys=True,
                separators=(",", ":"),
                default=str,
            ).encode()
        ).hexdigest()
        return BillingAccountCreditAllocationPreviewRead(
            billing_account_id=account.id,
            subscriber_id=subscriber.id,
            currency=account.currency,
            recorded_consolidated_credit=recorded,
            evidenced_consolidated_credit=evidenced,
            unbacked_consolidated_credit=round_money(recorded - evidenced),
            subscriber_receivable_before=receivable_before,
            allocation_amount=amount,
            subscriber_receivable_after=round_money(receivable_before - amount),
            source_effects=source_effects,
            invoice_effects=invoice_effects,
            service_access_consequence=str(
                fingerprint_payload["service_access_consequence"]
            ),
            fingerprint=fingerprint,
        )

    @staticmethod
    def _result(
        allocation: BillingAccountCreditAllocation, *, replay: bool
    ) -> BillingAccountCreditAllocationResultRead:
        return BillingAccountCreditAllocationResultRead(
            allocation_id=allocation.id,
            billing_account_id=allocation.billing_account_id,
            subscriber_id=allocation.subscriber_id,
            amount=allocation.amount,
            currency=allocation.currency,
            billing_account_ledger_entry_id=allocation.billing_account_ledger_entry_id,
            payment_allocation_ids=[
                item.payment_allocation_id for item in allocation.items
            ],
            subscriber_ledger_entry_ids=[
                item.subscriber_ledger_entry_id for item in allocation.items
            ],
            service_access_consequence=(
                "request_reconciliation_for_paid_member_invoices_no_direct_access_decision"
            ),
            idempotent_replay=replay,
        )

    @staticmethod
    def _replay(
        db: Session,
        *,
        key: str,
        fingerprint: str,
    ) -> BillingAccountCreditAllocationResultRead | None:
        reservation = (
            db.query(IdempotencyKey)
            .filter(IdempotencyKey.scope == _CREDIT_ALLOCATION_IDEMPOTENCY_SCOPE)
            .filter(IdempotencyKey.key == key)
            .first()
        )
        if reservation is None:
            return None
        if not reservation.ref_id:
            raise HTTPException(
                status_code=409, detail="Consolidated credit allocation is in progress"
            )
        allocation = get_by_id(db, BillingAccountCreditAllocation, reservation.ref_id)
        if allocation is None:
            raise HTTPException(
                status_code=409,
                detail="Consolidated credit allocation evidence is incomplete",
            )
        if allocation.preview_fingerprint != fingerprint:
            raise HTTPException(
                status_code=409,
                detail="Idempotency key belongs to a different allocation preview",
            )
        return ConsolidatedCreditAllocations._result(allocation, replay=True)

    @classmethod
    def confirm(
        cls,
        db: Session,
        billing_account_id: str,
        subscriber_id: str,
        command: BillingAccountCreditAllocationConfirm,
        *,
        actor_id: str | None = None,
        commit: bool = True,
    ) -> BillingAccountCreditAllocationResultRead:
        key = _normalize_key(command.idempotency_key)
        replay = cls._replay(db, key=key, fingerprint=command.preview_fingerprint)
        if replay is not None:
            return replay
        account = _lock_billing_account(db, billing_account_id)
        subscriber = (
            db.query(Subscriber)
            .filter(Subscriber.id == subscriber_id)
            .with_for_update()
            .first()
        )
        if subscriber is None or subscriber.reseller_id != account.reseller_id:
            raise HTTPException(status_code=404, detail="Subscriber not found")
        (
            db.query(BillingAccountLedgerEntry)
            .filter(BillingAccountLedgerEntry.billing_account_id == account.id)
            .with_for_update()
            .all()
        )
        (
            db.query(Invoice)
            .filter(Invoice.account_id == subscriber.id)
            .filter(Invoice.is_active.is_(True))
            .filter(Invoice.status.in_(_OPEN_INVOICE_STATUSES))
            .with_for_update()
            .all()
        )
        request = BillingAccountCreditAllocationPreviewRequest(amount=command.amount)
        try:
            preview = cls.preview(db, str(account.id), str(subscriber.id), request)
        except HTTPException as exc:
            raise HTTPException(
                status_code=409,
                detail="Financial state changed after preview; preview again",
            ) from exc
        if preview.fingerprint != command.preview_fingerprint:
            raise HTTPException(
                status_code=409,
                detail="Financial state changed after preview; preview again",
            )
        replay = cls._replay(db, key=key, fingerprint=command.preview_fingerprint)
        if replay is not None:
            return replay
        reservation = IdempotencyKey(
            scope=_CREDIT_ALLOCATION_IDEMPOTENCY_SCOPE,
            key=key,
        )
        db.add(reservation)
        try:
            account.balance = round_money(
                preview.evidenced_consolidated_credit - preview.allocation_amount
            )
            account.updated_at = datetime.now(UTC)
            debit_entry = BillingAccountLedgerEntry(
                billing_account_id=account.id,
                payment_id=None,
                entry_type=LedgerEntryType.debit,
                source=LedgerSource.payment,
                amount=preview.allocation_amount,
                currency=preview.currency,
                balance_after=account.balance,
                memo=f"Consolidated credit allocated to subscriber {subscriber.id}",
            )
            db.add(debit_entry)
            db.flush()
            allocation = BillingAccountCreditAllocation(
                billing_account_id=account.id,
                subscriber_id=subscriber.id,
                billing_account_ledger_entry_id=debit_entry.id,
                amount=preview.allocation_amount,
                currency=preview.currency,
                preview_fingerprint=preview.fingerprint,
                idempotency_key=key,
            )
            db.add(allocation)
            db.flush()
            allocations_by_payment: dict[Payment, list[PaymentAllocation]] = {}
            touched_invoice_ids: set = set()
            for effect in preview.invoice_effects:
                payment = get_by_id(db, Payment, effect.source_payment_id)
                invoice = get_by_id(db, Invoice, effect.invoice_id)
                if payment is None or invoice is None:
                    raise HTTPException(
                        status_code=409,
                        detail="Confirmed allocation source evidence disappeared",
                    )
                payment_allocation, applied = _apply_payment_allocation(
                    db,
                    payment,
                    invoice,
                    effect.allocation_amount,
                    memo="Allocated from evidenced consolidated credit",
                )
                if (
                    applied != effect.allocation_amount
                    or payment_allocation.ledger_entry_id is None
                ):
                    raise HTTPException(
                        status_code=409,
                        detail="Subscriber allocation result no longer matches preview",
                    )
                payment_allocation.preview_fingerprint = preview.fingerprint
                db.add(
                    BillingAccountCreditAllocationItem(
                        allocation_id=allocation.id,
                        source_billing_account_ledger_entry_id=(
                            effect.source_billing_account_ledger_entry_id
                        ),
                        payment_allocation_id=payment_allocation.id,
                        subscriber_ledger_entry_id=payment_allocation.ledger_entry_id,
                        amount=effect.allocation_amount,
                    )
                )
                allocations_by_payment.setdefault(payment, []).append(
                    payment_allocation
                )
                touched_invoice_ids.add(invoice.id)
            db.flush()
            for invoice_id in touched_invoice_ids:
                invoice = get_by_id(db, Invoice, invoice_id)
                if invoice is not None:
                    _finalize_invoice_payment_effects(db, invoice)
            AuditEvents.stage(
                db,
                AuditEventCreate(
                    actor_type=(
                        AuditActorType.user if actor_id else AuditActorType.system
                    ),
                    actor_id=actor_id,
                    action="allocate_consolidated_credit",
                    entity_type="billing_account_credit_allocation",
                    entity_id=str(allocation.id),
                    metadata_={
                        "billing_account_id": str(account.id),
                        "subscriber_id": str(subscriber.id),
                        "amount": str(preview.allocation_amount),
                        "currency": preview.currency,
                        "preview_fingerprint": preview.fingerprint,
                        "billing_account_ledger_entry_id": str(debit_entry.id),
                        "source_billing_account_ledger_entry_ids": [
                            str(effect.billing_account_ledger_entry_id)
                            for effect in preview.source_effects
                        ],
                        "subscriber_ledger_entry_ids": [
                            str(item.subscriber_ledger_entry_id)
                            for item in allocation.items
                        ],
                        "service_access_consequence": (
                            preview.service_access_consequence
                        ),
                    },
                ),
            )
            reservation.ref_id = str(allocation.id)
            for payment, payment_allocations in allocations_by_payment.items():
                _emit_consolidated_payment_events(db, payment, payment_allocations)
            db.flush()
            if commit:
                db.commit()
                db.refresh(allocation)
            return cls._result(allocation, replay=False)
        except IntegrityError as exc:
            db.rollback()
            replay = cls._replay(db, key=key, fingerprint=command.preview_fingerprint)
            if replay is not None:
                return replay
            raise HTTPException(
                status_code=409,
                detail="Consolidated credit allocation is already recorded",
            ) from exc
        except Exception:
            db.rollback()
            raise


consolidated_payment_settlements = ConsolidatedPaymentSettlements()
consolidated_credit_allocations = ConsolidatedCreditAllocations()
consolidated_payment_refunds = ConsolidatedPaymentRefunds()
consolidated_payment_reversals = ConsolidatedPaymentReversals()
