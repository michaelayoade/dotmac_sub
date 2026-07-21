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
from uuid import UUID, uuid4

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
    ConsolidatedCreditConsumptionReconciliationEvidence,
    ConsolidatedPaymentReturnAllocationEvidence,
    ConsolidatedPaymentReturnDocumentReconstructionEvidence,
    ConsolidatedPaymentReturnReconciliationEvidence,
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
    BillingAccountCreditConsumptionAllocationCandidateRead,
    BillingAccountCreditConsumptionDebitCandidateRead,
    BillingAccountCreditConsumptionEffectRead,
    BillingAccountCreditConsumptionEvidenceInspectionRead,
    BillingAccountCreditConsumptionReconciliationConfirm,
    BillingAccountCreditConsumptionReconciliationPreviewRead,
    BillingAccountCreditConsumptionReconciliationRequest,
    BillingAccountCreditConsumptionReconciliationResultRead,
    BillingAccountCreditConsumptionSourceCandidateRead,
    BillingAccountCreditInvoiceEffectRead,
    BillingAccountCreditSourceEffectRead,
    BillingAccountLedgerEvidenceCandidateRead,
    BillingAccountMissingPaymentReturnEvidenceInspectionRead,
    BillingAccountPaymentAllocationEffectRead,
    BillingAccountPaymentConfirm,
    BillingAccountPaymentPreviewRead,
    BillingAccountPaymentPreviewRequest,
    BillingAccountPaymentProvenanceCandidateRead,
    BillingAccountPaymentRefundPreviewRead,
    BillingAccountPaymentRefundRequest,
    BillingAccountPaymentReturnAllocationCandidateRead,
    BillingAccountPaymentReturnAllocationEvidenceRead,
    BillingAccountPaymentReturnDocumentReconstructionConfirm,
    BillingAccountPaymentReturnDocumentReconstructionPreviewRead,
    BillingAccountPaymentReturnDocumentReconstructionRequest,
    BillingAccountPaymentReturnDocumentReconstructionResultRead,
    BillingAccountPaymentReturnEvidenceInspectionRead,
    BillingAccountPaymentReturnInvoiceEffectRead,
    BillingAccountPaymentReturnReconciliationConfirm,
    BillingAccountPaymentReturnReconciliationPreviewRead,
    BillingAccountPaymentReturnReconciliationRequest,
    BillingAccountPaymentReturnReconciliationResultRead,
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
_CREDIT_RECONCILIATION_IDEMPOTENCY_SCOPE = (
    "consolidated_credit_consumption_reconciliation"
)
_REFUND_IDEMPOTENCY_SCOPE = "consolidated_payment_refund"
_REVERSAL_IDEMPOTENCY_SCOPE = "consolidated_payment_reversal"
_RETURN_RECONCILIATION_IDEMPOTENCY_SCOPE = "consolidated_return_reconciliation"
_RETURN_DOCUMENT_RECONSTRUCTION_IDEMPOTENCY_SCOPE = (
    "consolidated_return_document_reconstruction"
)
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
    linked_consumption: Decimal
    returned_amount: Decimal
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


def _credit_source_positions(
    db: Session,
    account: BillingAccount,
    *,
    strict: bool,
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
            if strict:
                raise HTTPException(
                    status_code=409,
                    detail="Consolidated credit source has no exact payment evidence",
                )
            continue
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
            if strict:
                raise HTTPException(
                    status_code=409,
                    detail=(
                        "Consolidated credit source settlement evidence is incomplete"
                    ),
                )
            continue
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
        available = round_money(
            to_decimal(entry.amount) - linked_consumption - returned_credit
        )
        if available < Decimal("0.00"):
            if strict:
                raise HTTPException(
                    status_code=409,
                    detail="Consolidated credit consumption exceeds its source entry",
                )
            continue
        sources.append(
            _CreditSourcePosition(
                entry=entry,
                payment=payment,
                linked_consumption=linked_consumption,
                returned_amount=returned_credit,
                available=available,
            )
        )
    return sources


def _linked_carrier_consumption(db: Session, payment_id: UUID) -> Decimal:
    return round_money(
        to_decimal(
            db.query(
                func.coalesce(
                    func.sum(BillingAccountCreditAllocationItem.amount),
                    Decimal("0.00"),
                )
            )
            .join(
                PaymentAllocation,
                PaymentAllocation.id
                == BillingAccountCreditAllocationItem.payment_allocation_id,
            )
            .filter(PaymentAllocation.payment_id == payment_id)
            .filter(PaymentAllocation.is_active.is_(True))
            .scalar()
        )
    )


def _later_allocation_gap(db: Session, payment: Payment) -> Decimal:
    settlement = payment.settlement
    if settlement is None:
        raise HTTPException(
            status_code=409,
            detail="Historical allocation carrier has no exact settlement evidence",
        )
    active_allocated = round_money(
        to_decimal(
            db.query(func.coalesce(func.sum(PaymentAllocation.amount), Decimal("0.00")))
            .filter(PaymentAllocation.payment_id == payment.id)
            .filter(PaymentAllocation.is_active.is_(True))
            .scalar()
        )
    )
    initially_allocated = round_money(
        to_decimal(settlement.amount)
        - to_decimal(settlement.unallocated_amount)
        - to_decimal(settlement.prepaid_amount)
    )
    later_allocated = max(
        Decimal("0.00"), round_money(active_allocated - initially_allocated)
    )
    linked_carrier = _linked_carrier_consumption(db, payment.id)
    gap = round_money(later_allocated - linked_carrier)
    if gap < Decimal("0.00"):
        raise HTTPException(
            status_code=409,
            detail="Consolidated allocation consumption exceeds later allocations",
        )
    return gap


def _credit_sources(
    db: Session, account: BillingAccount
) -> list[_CreditSourcePosition]:
    sources = _credit_source_positions(db, account, strict=True)
    for source in sources:
        if _later_allocation_gap(db, source.payment) != Decimal("0.00"):
            raise HTTPException(
                status_code=409,
                detail=(
                    "Consolidated credit has historical allocation without exact "
                    "consumption evidence; reconcile it before allocating"
                ),
            )
    return [source for source in sources if source.available > Decimal("0.00")]


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
    def stage_settle_verified(
        cls,
        db: Session,
        billing_account_id: str,
        request: BillingAccountPaymentPreviewRequest,
        *,
        idempotency_key: str,
        origin: PaymentSettlementOrigin,
        actor_id: str | None = None,
        existing_payment_id: str | None = None,
    ) -> ConsolidatedPaymentSettlementResult:
        """Stage a verified consolidated settlement in the caller's transaction."""

        preview = cls.preview(db, billing_account_id, request)
        command = BillingAccountPaymentConfirm(
            **request.model_dump(),
            preview_fingerprint=preview.fingerprint,
            idempotency_key=idempotency_key,
        )
        return cls.stage_confirm(
            db,
            billing_account_id,
            command,
            origin=origin,
            actor_id=actor_id,
            existing_payment_id=existing_payment_id,
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
    def stage_confirm(
        cls,
        db: Session,
        billing_account_id: str,
        command: BillingAccountPaymentConfirm,
        *,
        origin: PaymentSettlementOrigin = PaymentSettlementOrigin.manual,
        actor_id: str | None = None,
        existing_payment_id: str | None = None,
    ) -> ConsolidatedPaymentSettlementResult:
        """Stage one fingerprint-bound settlement without ending the transaction."""

        return cls._confirm(
            db,
            billing_account_id,
            command,
            origin=origin,
            actor_id=actor_id,
            complete_transaction=False,
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
        """Legacy root wrapper; coordinators use :meth:`stage_confirm`."""

        return cls._confirm(
            db,
            billing_account_id,
            command,
            origin=origin,
            actor_id=actor_id,
            complete_transaction=commit,
            existing_payment_id=existing_payment_id,
        )

    @classmethod
    def _confirm(
        cls,
        db: Session,
        billing_account_id: str,
        command: BillingAccountPaymentConfirm,
        *,
        origin: PaymentSettlementOrigin,
        actor_id: str | None,
        complete_transaction: bool,
        existing_payment_id: str | None,
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
                    or round_money(to_decimal(payment.provider_fee))
                    != round_money(to_decimal(command.provider_fee))
                    or payment.currency != preview.currency
                    or payment.provider_id != command.provider_id
                    or payment.external_id != command.external_id
                ):
                    raise HTTPException(
                        status_code=409,
                        detail="Confirmed payment no longer matches its observation",
                    )
                payment.status = PaymentStatus.succeeded
                payment.provider_fee = command.provider_fee
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
                    provider_fee=command.provider_fee,
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
            if complete_transaction:
                db.commit()
                db.refresh(payment)
                db.refresh(settlement)
            return ConsolidatedPaymentSettlementResult(
                payment=payment,
                settlement=settlement,
                preview=preview,
            )
        except IntegrityError as exc:
            if not complete_transaction:
                raise
            db.rollback()
            replay = cls._replay(db, key=key, fingerprint=command.preview_fingerprint)
            if replay is not None:
                return replay
            raise HTTPException(
                status_code=409, detail="Consolidated payment is already recorded"
            ) from exc
        except Exception:
            if complete_transaction:
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


class ConsolidatedPaymentReturnReconciliations:
    """Owner for linking exact evidence to historical consolidated returns."""

    @staticmethod
    def _record(
        db: Session,
        *,
        payment_id: str,
        return_type: str,
        return_id: str,
        lock: bool = False,
    ) -> tuple[Payment, PaymentRefund | PaymentReversal]:
        payment = (
            lock_for_update(db, Payment, payment_id)
            if lock
            else get_by_id(db, Payment, payment_id)
        )
        if (
            payment is None
            or payment.billing_account_id is None
            or payment.account_id is not None
        ):
            raise HTTPException(
                status_code=404, detail="Consolidated payment not found"
            )
        record: PaymentRefund | PaymentReversal | None
        if return_type == "refund":
            record = (
                lock_for_update(db, PaymentRefund, return_id)
                if lock
                else get_by_id(db, PaymentRefund, return_id)
            )
        elif return_type == "reversal":
            record = (
                lock_for_update(db, PaymentReversal, return_id)
                if lock
                else get_by_id(db, PaymentReversal, return_id)
            )
        else:
            raise HTTPException(
                status_code=400, detail="Return type must be refund or reversal"
            )
        if record is None or record.payment_id != payment.id:
            raise HTTPException(status_code=404, detail="Payment return not found")
        return payment, record

    @staticmethod
    def _source(return_type: str) -> LedgerSource:
        return LedgerSource.refund if return_type == "refund" else LedgerSource.payment

    @classmethod
    def inspect_missing_document_evidence(
        cls,
        db: Session,
        payment_id: str,
        return_type: str,
    ) -> BillingAccountMissingPaymentReturnEvidenceInspectionRead:
        """List unclaimed facts without treating a historical status as evidence."""
        if return_type not in {"refund", "reversal"}:
            raise HTTPException(
                status_code=400, detail="Return type must be refund or reversal"
            )
        payment = get_by_id(db, Payment, payment_id)
        if (
            payment is None
            or payment.billing_account_id is None
            or payment.account_id is not None
        ):
            raise HTTPException(
                status_code=404, detail="Consolidated payment not found"
            )
        account = get_by_id(db, BillingAccount, payment.billing_account_id)
        if account is None:
            raise HTTPException(status_code=409, detail="Billing account not found")
        gross = round_money(to_decimal(payment.amount))
        expected_source = cls._source(return_type)
        billing_rows = (
            db.query(BillingAccountLedgerEntry)
            .filter(BillingAccountLedgerEntry.billing_account_id == account.id)
            .filter(BillingAccountLedgerEntry.payment_id == payment.id)
            .filter(BillingAccountLedgerEntry.entry_type == LedgerEntryType.debit)
            .filter(BillingAccountLedgerEntry.source == expected_source)
            .filter(BillingAccountLedgerEntry.currency == payment.currency)
            .filter(BillingAccountLedgerEntry.amount <= gross)
            .filter(BillingAccountLedgerEntry.is_active.is_(True))
            .order_by(
                BillingAccountLedgerEntry.created_at.asc(),
                BillingAccountLedgerEntry.id.asc(),
            )
            .all()
        )
        billing_candidates = [
            BillingAccountLedgerEvidenceCandidateRead(
                billing_account_ledger_entry_id=row.id,
                entry_type=row.entry_type,
                source=row.source,
                amount=round_money(to_decimal(row.amount)),
                currency=row.currency,
                balance_after=round_money(to_decimal(row.balance_after)),
            )
            for row in billing_rows
            if (
                db.query(PaymentRefund.id)
                .filter(PaymentRefund.billing_account_ledger_entry_id == row.id)
                .first()
                is None
                and db.query(PaymentReversal.id)
                .filter(PaymentReversal.billing_account_ledger_entry_id == row.id)
                .first()
                is None
            )
        ]

        allocation_candidates: list[
            BillingAccountPaymentReturnAllocationCandidateRead
        ] = []
        for allocation in sorted(
            payment.allocations,
            key=lambda item: (item.created_at, item.id),
        ):
            invoice = get_by_id(db, Invoice, allocation.invoice_id)
            if invoice is None:
                continue
            rows = (
                db.query(LedgerEntry)
                .filter(LedgerEntry.payment_id == payment.id)
                .filter(LedgerEntry.invoice_id == invoice.id)
                .filter(LedgerEntry.account_id == invoice.account_id)
                .filter(LedgerEntry.entry_type == LedgerEntryType.debit)
                .filter(LedgerEntry.source == expected_source)
                .filter(LedgerEntry.currency == payment.currency)
                .filter(LedgerEntry.amount == allocation.amount)
                .filter(LedgerEntry.is_active.is_(True))
                .order_by(LedgerEntry.created_at.asc(), LedgerEntry.id.asc())
                .all()
            )
            available_ids = [
                row.id
                for row in rows
                if (
                    db.query(ConsolidatedPaymentReturnAllocationEvidence.id)
                    .filter(
                        ConsolidatedPaymentReturnAllocationEvidence.ledger_entry_id
                        == row.id
                    )
                    .first()
                    is None
                    and db.query(ConsolidatedPaymentReturnAllocationEvidence.id)
                    .filter(
                        ConsolidatedPaymentReturnAllocationEvidence.payment_allocation_id
                        == allocation.id
                    )
                    .first()
                    is None
                )
            ]
            allocation_candidates.append(
                BillingAccountPaymentReturnAllocationCandidateRead(
                    payment_allocation_id=allocation.id,
                    invoice_id=invoice.id,
                    account_id=invoice.account_id,
                    amount=round_money(to_decimal(allocation.amount)),
                    allocation_active=allocation.is_active,
                    candidate_ledger_entry_ids=available_ids,
                )
            )

        expected_effect = (
            PaymentProviderEventFinancialEffect.refund_confirmed
            if return_type == "refund"
            else PaymentProviderEventFinancialEffect.reversal_confirmed
        )
        provider_rows = (
            db.query(PaymentProviderEvent)
            .filter(PaymentProviderEvent.payment_id == payment.id)
            .filter(PaymentProviderEvent.financial_effect == expected_effect)
            .filter(PaymentProviderEvent.amount.is_not(None))
            .filter(PaymentProviderEvent.amount <= gross)
            .filter(PaymentProviderEvent.currency == payment.currency)
            .filter(PaymentProviderEvent.status == PaymentProviderEventStatus.processed)
            .order_by(
                PaymentProviderEvent.received_at.asc(),
                PaymentProviderEvent.id.asc(),
            )
            .all()
        )
        provider_candidates = [
            BillingAccountPaymentProvenanceCandidateRead(
                provenance_type="provider_event",
                provenance_id=row.id,
                status=row.status.value,
                amount=round_money(to_decimal(row.amount)),
                currency=row.currency or payment.currency,
            )
            for row in provider_rows
            if (
                db.query(PaymentRefund.id)
                .filter(PaymentRefund.provider_event_id == row.id)
                .first()
                is None
                and db.query(PaymentReversal.id)
                .filter(PaymentReversal.provider_event_id == row.id)
                .first()
                is None
            )
        ]
        recorded = round_money(to_decimal(account.balance))
        evidenced = _billing_account_evidenced_balance(db, account)
        settlement = payment.settlement
        exact_settlement = (
            settlement is not None
            and settlement.currency == payment.currency
            and round_money(to_decimal(settlement.amount)) == gross
        )
        status_only_candidate = exact_settlement and (
            (
                return_type == "refund"
                and payment.status
                in {PaymentStatus.partially_refunded, PaymentStatus.refunded}
                and payment.reversal is None
            )
            or (
                return_type == "reversal"
                and payment.status == PaymentStatus.reversed
                and payment.reversal is None
            )
        )
        return BillingAccountMissingPaymentReturnEvidenceInspectionRead(
            return_type=return_type,
            payment_id=payment.id,
            billing_account_id=account.id,
            payment_state=payment.status,
            payment_amount=gross,
            currency=payment.currency,
            existing_refund_ids=[item.id for item in payment.refunds],
            existing_reversal_id=(
                payment.reversal.id if payment.reversal is not None else None
            ),
            status_only_candidate=status_only_candidate,
            recorded_consolidated_credit=recorded,
            evidenced_consolidated_credit=evidenced,
            projection_drift=round_money(recorded - evidenced),
            billing_account_candidate_entries=billing_candidates,
            allocation_candidates=allocation_candidates,
            provider_candidates=provider_candidates,
            service_access_consequence=(
                "none_missing_return_inspection_no_access_decision"
            ),
        )

    @classmethod
    def inspect_evidence(
        cls,
        db: Session,
        payment_id: str,
        return_type: str,
        return_id: str,
    ) -> BillingAccountPaymentReturnEvidenceInspectionRead:
        payment, record = cls._record(
            db,
            payment_id=payment_id,
            return_type=return_type,
            return_id=return_id,
        )
        account = get_by_id(db, BillingAccount, payment.billing_account_id)
        if account is None:
            raise HTTPException(status_code=409, detail="Billing account not found")
        amount = round_money(to_decimal(record.amount))
        expected_source = cls._source(return_type)
        billing_rows = (
            db.query(BillingAccountLedgerEntry)
            .filter(BillingAccountLedgerEntry.billing_account_id == account.id)
            .filter(BillingAccountLedgerEntry.payment_id == payment.id)
            .filter(BillingAccountLedgerEntry.entry_type == LedgerEntryType.debit)
            .filter(BillingAccountLedgerEntry.source == expected_source)
            .filter(BillingAccountLedgerEntry.currency == payment.currency)
            .filter(BillingAccountLedgerEntry.amount <= amount)
            .filter(BillingAccountLedgerEntry.is_active.is_(True))
            .order_by(
                BillingAccountLedgerEntry.created_at.asc(),
                BillingAccountLedgerEntry.id.asc(),
            )
            .all()
        )
        billing_candidates: list[BillingAccountLedgerEvidenceCandidateRead] = []
        for billing_row in billing_rows:
            refund_claim_query = db.query(PaymentRefund.id).filter(
                PaymentRefund.billing_account_ledger_entry_id == billing_row.id
            )
            reversal_claim_query = db.query(PaymentReversal.id).filter(
                PaymentReversal.billing_account_ledger_entry_id == billing_row.id
            )
            if isinstance(record, PaymentRefund):
                refund_claim_query = refund_claim_query.filter(
                    PaymentRefund.id != record.id
                )
            else:
                reversal_claim_query = reversal_claim_query.filter(
                    PaymentReversal.id != record.id
                )
            refund_claim = refund_claim_query.first()
            reversal_claim = reversal_claim_query.first()
            if refund_claim is not None or reversal_claim is not None:
                continue
            billing_candidates.append(
                BillingAccountLedgerEvidenceCandidateRead(
                    billing_account_ledger_entry_id=billing_row.id,
                    entry_type=billing_row.entry_type,
                    source=billing_row.source,
                    amount=round_money(to_decimal(billing_row.amount)),
                    currency=billing_row.currency,
                    balance_after=round_money(to_decimal(billing_row.balance_after)),
                )
            )

        allocation_candidates: list[
            BillingAccountPaymentReturnAllocationCandidateRead
        ] = []
        for allocation in sorted(
            payment.allocations,
            key=lambda item: (item.created_at, item.id),
        ):
            invoice = get_by_id(db, Invoice, allocation.invoice_id)
            if invoice is None:
                continue
            candidate_rows = (
                db.query(LedgerEntry)
                .filter(LedgerEntry.payment_id == payment.id)
                .filter(LedgerEntry.invoice_id == invoice.id)
                .filter(LedgerEntry.account_id == invoice.account_id)
                .filter(LedgerEntry.entry_type == LedgerEntryType.debit)
                .filter(LedgerEntry.source == expected_source)
                .filter(LedgerEntry.currency == payment.currency)
                .filter(LedgerEntry.amount == allocation.amount)
                .filter(LedgerEntry.is_active.is_(True))
                .order_by(LedgerEntry.created_at.asc(), LedgerEntry.id.asc())
                .all()
            )
            available_ids: list[UUID] = []
            for candidate_row in candidate_rows:
                claim = (
                    db.query(ConsolidatedPaymentReturnAllocationEvidence)
                    .filter(
                        ConsolidatedPaymentReturnAllocationEvidence.ledger_entry_id
                        == candidate_row.id
                    )
                    .first()
                )
                if (
                    claim is None
                    or (return_type == "refund" and claim.refund_id == record.id)
                    or (return_type == "reversal" and claim.reversal_id == record.id)
                ):
                    available_ids.append(candidate_row.id)
            allocation_candidates.append(
                BillingAccountPaymentReturnAllocationCandidateRead(
                    payment_allocation_id=allocation.id,
                    invoice_id=invoice.id,
                    account_id=invoice.account_id,
                    amount=round_money(to_decimal(allocation.amount)),
                    allocation_active=allocation.is_active,
                    candidate_ledger_entry_ids=available_ids,
                )
            )

        expected_effect = (
            PaymentProviderEventFinancialEffect.refund_confirmed
            if return_type == "refund"
            else PaymentProviderEventFinancialEffect.reversal_confirmed
        )
        provider_rows = (
            db.query(PaymentProviderEvent)
            .filter(PaymentProviderEvent.payment_id == payment.id)
            .filter(PaymentProviderEvent.financial_effect == expected_effect)
            .filter(PaymentProviderEvent.amount == amount)
            .filter(PaymentProviderEvent.currency == payment.currency)
            .filter(PaymentProviderEvent.status == PaymentProviderEventStatus.processed)
            .order_by(
                PaymentProviderEvent.received_at.asc(),
                PaymentProviderEvent.id.asc(),
            )
            .all()
        )
        provider_candidates: list[BillingAccountPaymentProvenanceCandidateRead] = []
        for provider_row in provider_rows:
            refund_claim_query = db.query(PaymentRefund.id).filter(
                PaymentRefund.provider_event_id == provider_row.id
            )
            reversal_claim_query = db.query(PaymentReversal.id).filter(
                PaymentReversal.provider_event_id == provider_row.id
            )
            if isinstance(record, PaymentRefund):
                refund_claim_query = refund_claim_query.filter(
                    PaymentRefund.id != record.id
                )
            else:
                reversal_claim_query = reversal_claim_query.filter(
                    PaymentReversal.id != record.id
                )
            if (
                provider_row.amount is None
                or refund_claim_query.first() is not None
                or reversal_claim_query.first() is not None
            ):
                continue
            provider_candidates.append(
                BillingAccountPaymentProvenanceCandidateRead(
                    provenance_type="provider_event",
                    provenance_id=provider_row.id,
                    status=provider_row.status.value,
                    amount=round_money(to_decimal(provider_row.amount)),
                    currency=provider_row.currency or payment.currency,
                )
            )
        linked_allocation_evidence = list(record.consolidated_allocation_evidence)
        recorded = round_money(to_decimal(account.balance))
        evidenced = _billing_account_evidenced_balance(db, account)
        return BillingAccountPaymentReturnEvidenceInspectionRead(
            return_type=return_type,
            return_id=record.id,
            payment_id=payment.id,
            billing_account_id=account.id,
            payment_state=payment.status,
            return_amount=amount,
            currency=payment.currency,
            already_reconciled=(
                record.consolidated_reconciliation_evidence is not None
                and (
                    _refund_evidence_total(record)
                    if isinstance(record, PaymentRefund)
                    else _reversal_evidence_total(record)
                )
                == amount
            ),
            recorded_consolidated_credit=recorded,
            evidenced_consolidated_credit=evidenced,
            projection_drift=round_money(recorded - evidenced),
            linked_billing_account_ledger_entry_id=(
                record.billing_account_ledger_entry_id
            ),
            linked_allocation_evidence_ids=[
                item.id for item in linked_allocation_evidence
            ],
            linked_provider_event_id=record.provider_event_id,
            billing_account_candidate_entries=billing_candidates,
            allocation_candidates=allocation_candidates,
            provider_candidates=provider_candidates,
            service_access_consequence="none_inspection_only_no_access_decision",
        )

    @classmethod
    def _build_preview(
        cls,
        db: Session,
        payment_id: str,
        return_type: str,
        return_id: str,
        request: BillingAccountPaymentReturnReconciliationRequest,
        *,
        lock: bool = False,
        record_override: PaymentRefund | PaymentReversal | None = None,
        record_is_new: bool = False,
    ) -> BillingAccountPaymentReturnReconciliationPreviewRead:
        if record_override is None:
            payment, record = cls._record(
                db,
                payment_id=payment_id,
                return_type=return_type,
                return_id=return_id,
                lock=lock,
            )
        else:
            candidate_payment = (
                lock_for_update(db, Payment, payment_id)
                if lock
                else get_by_id(db, Payment, payment_id)
            )
            record = record_override
            if (
                candidate_payment is None
                or candidate_payment.billing_account_id is None
                or candidate_payment.account_id is not None
            ):
                raise HTTPException(
                    status_code=404, detail="Consolidated payment not found"
                )
            payment = candidate_payment
            if (
                record.payment_id != payment.id
                or str(record.id) != return_id
                or (return_type == "refund" and not isinstance(record, PaymentRefund))
                or (
                    return_type == "reversal"
                    and not isinstance(record, PaymentReversal)
                )
            ):
                raise HTTPException(
                    status_code=409,
                    detail="Proposed return document does not match the request",
                )
        assert payment.billing_account_id is not None
        account = (
            lock_for_update(db, BillingAccount, payment.billing_account_id)
            if lock
            else get_by_id(db, BillingAccount, payment.billing_account_id)
        )
        if account is None:
            raise HTTPException(status_code=409, detail="Billing account not found")
        if lock:
            (
                db.query(BillingAccountLedgerEntry)
                .filter(BillingAccountLedgerEntry.billing_account_id == account.id)
                .with_for_update()
                .all()
            )
            (
                db.query(PaymentAllocation)
                .filter(PaymentAllocation.payment_id == payment.id)
                .with_for_update()
                .all()
            )
        reason = request.reason.strip()
        if len(reason) < 10:
            raise HTTPException(
                status_code=400, detail="A reviewed reconciliation reason is required"
            )
        settlement = payment.settlement
        gross = round_money(to_decimal(payment.amount))
        amount = round_money(to_decimal(record.amount))
        if (
            not payment.is_active
            or settlement is None
            or settlement.currency != payment.currency
            or round_money(to_decimal(settlement.amount)) != gross
            or record.currency != payment.currency
            or amount <= Decimal("0.00")
            or amount > gross
            or record.ledger_entry_id is not None
            or record.credit_consumption_ledger_entry_id is not None
        ):
            raise HTTPException(
                status_code=409,
                detail="Historical consolidated return document is not structurally eligible",
            )
        if record.consolidated_reconciliation_evidence is not None:
            raise HTTPException(
                status_code=409, detail="Return evidence is already reconciled"
            )
        if record.preview_fingerprint is not None:
            raise HTTPException(
                status_code=409,
                detail="Owner-confirmed return with missing evidence requires incident review",
            )

        recorded = round_money(to_decimal(account.balance))
        evidenced = _billing_account_evidenced_balance(db, account)
        drift = round_money(recorded - evidenced)
        if drift != Decimal("0.00"):
            raise HTTPException(
                status_code=409,
                detail=(
                    "Billing-account projection drift must be reconciled before "
                    "return evidence"
                ),
            )

        refunded_documents = round_money(
            sum(
                (round_money(to_decimal(item.amount)) for item in payment.refunds),
                Decimal("0.00"),
            )
        )
        if record_is_new and return_type == "refund":
            refunded_documents = round_money(refunded_documents + amount)
        if refunded_documents < Decimal("0.00") or refunded_documents > gross:
            raise HTTPException(
                status_code=409, detail="Historical refund documents exceed the payment"
            )
        if return_type == "reversal" and amount != round_money(
            gross - refunded_documents
        ):
            raise HTTPException(
                status_code=409,
                detail="Historical reversal amount does not match remaining payment value",
            )
        refunded_after = refunded_documents
        if return_type == "reversal" or payment.reversal is not None:
            state_after = PaymentStatus.reversed
        else:
            state_after = (
                PaymentStatus.refunded
                if refunded_after == gross
                else PaymentStatus.partially_refunded
            )

        expected_source = cls._source(return_type)
        billing_entry: BillingAccountLedgerEntry | None = None
        billing_amount = Decimal("0.00")
        if request.billing_account_ledger_entry_id is not None:
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
                or billing_entry.entry_type != LedgerEntryType.debit
                or billing_entry.source != expected_source
                or billing_entry.currency != payment.currency
            ):
                raise HTTPException(
                    status_code=409,
                    detail="Selected billing-account return debit is not exact evidence",
                )
            billing_amount = round_money(to_decimal(billing_entry.amount))
            if billing_amount <= Decimal("0.00") or billing_amount > amount:
                raise HTTPException(
                    status_code=409,
                    detail="Selected billing-account return amount is invalid",
                )
            if (
                record.billing_account_ledger_entry_id is not None
                and record.billing_account_ledger_entry_id != billing_entry.id
            ):
                raise HTTPException(
                    status_code=409,
                    detail="Return document already names different billing evidence",
                )
            refund_claim_query = db.query(PaymentRefund.id).filter(
                PaymentRefund.billing_account_ledger_entry_id == billing_entry.id
            )
            reversal_claim_query = db.query(PaymentReversal.id).filter(
                PaymentReversal.billing_account_ledger_entry_id == billing_entry.id
            )
            if isinstance(record, PaymentRefund):
                refund_claim_query = refund_claim_query.filter(
                    PaymentRefund.id != record.id
                )
            else:
                reversal_claim_query = reversal_claim_query.filter(
                    PaymentReversal.id != record.id
                )
            refund_claim = refund_claim_query.first()
            reversal_claim = reversal_claim_query.first()
            if refund_claim is not None or reversal_claim is not None:
                raise HTTPException(
                    status_code=409,
                    detail="Selected billing-account return evidence is already claimed",
                )
        elif record.billing_account_ledger_entry_id is not None:
            raise HTTPException(
                status_code=409,
                detail="Existing billing-account evidence must be explicitly selected",
            )

        allocation_ids = list(request.allocation_ledger_entry_ids)
        ledger_ids = list(request.allocation_ledger_entry_ids.values())
        if len(ledger_ids) != len(set(ledger_ids)):
            raise HTTPException(
                status_code=409,
                detail="Return ledger selections must be unique",
            )
        existing_evidence = {
            item.payment_allocation_id: item
            for item in record.consolidated_allocation_evidence
        }
        if not set(existing_evidence).issubset(set(allocation_ids)):
            raise HTTPException(
                status_code=409,
                detail="Every existing allocation-return link must be selected",
            )
        effects: list[BillingAccountPaymentReturnAllocationEvidenceRead] = []
        allocation_amount = Decimal("0.00")
        for allocation_id in sorted(allocation_ids, key=str):
            allocation = (
                lock_for_update(db, PaymentAllocation, allocation_id)
                if lock
                else get_by_id(db, PaymentAllocation, allocation_id)
            )
            if (
                allocation is None
                or allocation.payment_id != payment.id
                or allocation.is_active
            ):
                raise HTTPException(
                    status_code=409,
                    detail=(
                        "Selected allocation is not an already-returned payment "
                        "allocation"
                    ),
                )
            invoice = get_by_id(db, Invoice, allocation.invoice_id)
            subscriber = (
                get_by_id(db, Subscriber, invoice.account_id)
                if invoice is not None
                else None
            )
            if (
                invoice is None
                or subscriber is None
                or subscriber.reseller_id != account.reseller_id
            ):
                raise HTTPException(
                    status_code=409,
                    detail="Selected return allocation is outside the billing account",
                )
            ledger_id = request.allocation_ledger_entry_ids[allocation.id]
            ledger_entry = (
                lock_for_update(db, LedgerEntry, ledger_id)
                if lock
                else get_by_id(db, LedgerEntry, ledger_id)
            )
            allocation_value = round_money(to_decimal(allocation.amount))
            if (
                ledger_entry is None
                or not ledger_entry.is_active
                or ledger_entry.payment_id != payment.id
                or ledger_entry.invoice_id != invoice.id
                or ledger_entry.account_id != invoice.account_id
                or ledger_entry.entry_type != LedgerEntryType.debit
                or ledger_entry.source != expected_source
                or ledger_entry.currency != payment.currency
                or round_money(to_decimal(ledger_entry.amount)) != allocation_value
            ):
                raise HTTPException(
                    status_code=409,
                    detail="Selected allocation-return ledger entry is not exact evidence",
                )
            claim = (
                db.query(ConsolidatedPaymentReturnAllocationEvidence)
                .filter(
                    ConsolidatedPaymentReturnAllocationEvidence.payment_allocation_id
                    == allocation.id
                )
                .first()
            )
            existing = existing_evidence.get(allocation.id)
            if claim is not None and (existing is None or claim.id != existing.id):
                raise HTTPException(
                    status_code=409,
                    detail="Selected payment allocation is already returned elsewhere",
                )
            if existing is not None and (
                existing.ledger_entry_id != ledger_entry.id
                or round_money(to_decimal(existing.amount)) != allocation_value
            ):
                raise HTTPException(
                    status_code=409,
                    detail="Existing allocation-return evidence differs from selection",
                )
            ledger_claim = (
                db.query(ConsolidatedPaymentReturnAllocationEvidence)
                .filter(
                    ConsolidatedPaymentReturnAllocationEvidence.ledger_entry_id
                    == ledger_entry.id
                )
                .first()
            )
            if ledger_claim is not None and (
                existing is None or ledger_claim.id != existing.id
            ):
                raise HTTPException(
                    status_code=409,
                    detail="Selected allocation-return ledger entry is already claimed",
                )
            allocation_amount = round_money(allocation_amount + allocation_value)
            effects.append(
                BillingAccountPaymentReturnAllocationEvidenceRead(
                    payment_allocation_id=allocation.id,
                    invoice_id=invoice.id,
                    account_id=invoice.account_id,
                    amount=allocation_value,
                    ledger_entry_id=ledger_entry.id,
                )
            )
        if round_money(billing_amount + allocation_amount) != amount:
            raise HTTPException(
                status_code=409,
                detail="Selected return evidence does not exactly partition the amount",
            )

        return cls._finalize_preview(
            db,
            payment=payment,
            record=record,
            return_type=return_type,
            request=request,
            reason=reason,
            state_after=state_after,
            refunded_after=refunded_after,
            account=account,
            recorded=recorded,
            evidenced=evidenced,
            drift=drift,
            amount=amount,
            billing_entry=billing_entry,
            billing_amount=billing_amount,
            allocation_amount=allocation_amount,
            effects=effects,
            lock=lock,
        )

    @classmethod
    def _finalize_preview(
        cls,
        db: Session,
        *,
        payment: Payment,
        record: PaymentRefund | PaymentReversal,
        return_type: str,
        request: BillingAccountPaymentReturnReconciliationRequest,
        reason: str,
        state_after: PaymentStatus,
        refunded_after: Decimal,
        account: BillingAccount,
        recorded: Decimal,
        evidenced: Decimal,
        drift: Decimal,
        amount: Decimal,
        billing_entry: BillingAccountLedgerEntry | None,
        billing_amount: Decimal,
        allocation_amount: Decimal,
        effects: list[BillingAccountPaymentReturnAllocationEvidenceRead],
        lock: bool,
    ) -> BillingAccountPaymentReturnReconciliationPreviewRead:
        provider_event: PaymentProviderEvent | None = None
        if payment.provider_id is not None:
            if lock and request.provider_event_id is not None:
                locked_event = (
                    db.query(PaymentProviderEvent)
                    .populate_existing()
                    .filter(PaymentProviderEvent.id == request.provider_event_id)
                    .with_for_update()
                    .one_or_none()
                )
                if locked_event is None:
                    raise HTTPException(
                        status_code=404,
                        detail="Payment provider event not found",
                    )
                provider_event = locked_event
            provider_event = (
                _validate_refund_provider_event(
                    db,
                    payment=payment,
                    origin=PaymentRefundOrigin.provider_event,
                    provider_event_id=request.provider_event_id,
                )
                if return_type == "refund"
                else _validate_reversal_provider_event(
                    db,
                    payment=payment,
                    origin=PaymentReversalOrigin.provider_event,
                    provider_event_id=request.provider_event_id,
                )
            )
            assert provider_event is not None
            if (
                provider_event.status != PaymentProviderEventStatus.processed
                or round_money(to_decimal(provider_event.amount)) != amount
            ):
                raise HTTPException(
                    status_code=409,
                    detail="Provider return event is not exact processed evidence",
                )
            if record.provider_event_id not in {None, provider_event.id}:
                raise HTTPException(
                    status_code=409,
                    detail="Return document already names different provider evidence",
                )
            refund_event_claim_query = db.query(PaymentRefund.id).filter(
                PaymentRefund.provider_event_id == provider_event.id
            )
            reversal_event_claim_query = db.query(PaymentReversal.id).filter(
                PaymentReversal.provider_event_id == provider_event.id
            )
            if isinstance(record, PaymentRefund):
                refund_event_claim_query = refund_event_claim_query.filter(
                    PaymentRefund.id != record.id
                )
            else:
                reversal_event_claim_query = reversal_event_claim_query.filter(
                    PaymentReversal.id != record.id
                )
            refund_event_claim = refund_event_claim_query.first()
            reversal_event_claim = reversal_event_claim_query.first()
            if refund_event_claim is not None or reversal_event_claim is not None:
                raise HTTPException(
                    status_code=409,
                    detail="Provider return evidence is already claimed",
                )
            origin = "provider_event"
        else:
            origin = "manual"
            if (
                request.provider_event_id is not None
                or record.provider_event_id is not None
            ):
                raise HTTPException(
                    status_code=409,
                    detail="Manual consolidated return cannot claim provider evidence",
                )

        values: dict[str, object] = {
            "kind": "historical_consolidated_return_evidence_reconciliation",
            "return_type": return_type,
            "return_id": str(record.id),
            "payment_id": str(payment.id),
            "billing_account_id": str(account.id),
            "currency": payment.currency,
            "return_amount": str(amount),
            "payment_state_before": payment.status.value,
            "payment_state_after": state_after.value,
            "payment_refunded_amount_before": str(
                round_money(to_decimal(payment.refunded_amount))
            ),
            "payment_refunded_amount_after": str(refunded_after),
            "recorded_consolidated_credit": str(recorded),
            "evidenced_consolidated_credit": str(evidenced),
            "projection_drift": str(drift),
            "billing_account_return_amount": str(billing_amount),
            "billing_account_ledger_entry_id": (
                str(billing_entry.id) if billing_entry is not None else None
            ),
            "allocation_return_amount": str(allocation_amount),
            "allocation_evidence": [
                effect.model_dump(mode="json") for effect in effects
            ],
            "provider_event_id": (
                str(provider_event.id) if provider_event is not None else None
            ),
            "origin": origin,
            "reason": reason,
            "money_posted": False,
            "billing_account_balance_changed": False,
            "service_access_consequence": (
                "none_historical_return_evidence_no_access_decision"
            ),
        }
        fingerprint = hashlib.sha256(
            json.dumps(
                values,
                sort_keys=True,
                separators=(",", ":"),
                default=str,
            ).encode()
        ).hexdigest()
        return BillingAccountPaymentReturnReconciliationPreviewRead(
            return_type=return_type,
            return_id=record.id,
            payment_id=payment.id,
            billing_account_id=account.id,
            currency=payment.currency,
            return_amount=amount,
            payment_state_before=payment.status,
            payment_state_after=state_after,
            payment_refunded_amount_before=round_money(
                to_decimal(payment.refunded_amount)
            ),
            payment_refunded_amount_after=refunded_after,
            recorded_consolidated_credit=recorded,
            evidenced_consolidated_credit=evidenced,
            projection_drift=drift,
            billing_account_return_amount=billing_amount,
            billing_account_ledger_entry_id=(
                billing_entry.id if billing_entry is not None else None
            ),
            allocation_return_amount=allocation_amount,
            allocation_evidence=effects,
            provider_event_id=(
                provider_event.id if provider_event is not None else None
            ),
            money_posted=False,
            billing_account_balance_changed=False,
            service_access_consequence=str(values["service_access_consequence"]),
            fingerprint=fingerprint,
        )

    @classmethod
    def preview(
        cls,
        db: Session,
        payment_id: str,
        return_type: str,
        return_id: str,
        request: BillingAccountPaymentReturnReconciliationRequest,
    ) -> BillingAccountPaymentReturnReconciliationPreviewRead:
        return cls._build_preview(db, payment_id, return_type, return_id, request)

    @staticmethod
    def _require_reconciled_existing_documents(payment: Payment) -> None:
        for refund in payment.refunds:
            if (
                refund.consolidated_reconciliation_evidence is None
                or _refund_evidence_total(refund)
                != round_money(to_decimal(refund.amount))
            ):
                raise HTTPException(
                    status_code=409,
                    detail=(
                        "Existing consolidated return documents must be reconciled "
                        "before a missing document is reconstructed"
                    ),
                )
        if payment.reversal is not None:
            reversal = payment.reversal
            if (
                reversal.consolidated_reconciliation_evidence is None
                or _reversal_evidence_total(reversal)
                != round_money(to_decimal(reversal.amount))
            ):
                raise HTTPException(
                    status_code=409,
                    detail=(
                        "Existing consolidated return documents must be reconciled "
                        "before a missing document is reconstructed"
                    ),
                )

    @classmethod
    def _build_document_reconstruction_preview(
        cls,
        db: Session,
        payment_id: str,
        return_type: str,
        request: BillingAccountPaymentReturnDocumentReconstructionRequest,
        *,
        proposed_return_id: UUID | None = None,
        lock: bool = False,
    ) -> BillingAccountPaymentReturnDocumentReconstructionPreviewRead:
        if return_type not in {"refund", "reversal"}:
            raise HTTPException(
                status_code=400, detail="Return type must be refund or reversal"
            )
        payment = (
            lock_for_update(db, Payment, payment_id)
            if lock
            else get_by_id(db, Payment, payment_id)
        )
        if (
            payment is None
            or payment.billing_account_id is None
            or payment.account_id is not None
        ):
            raise HTTPException(
                status_code=404, detail="Consolidated payment not found"
            )
        if return_type == "refund":
            if (
                payment.status
                not in {
                    PaymentStatus.partially_refunded,
                    PaymentStatus.refunded,
                }
                or payment.reversal is not None
            ):
                raise HTTPException(
                    status_code=409,
                    detail=(
                        "Historical payment state is not consistent with a missing "
                        "refund document"
                    ),
                )
        elif payment.status != PaymentStatus.reversed or payment.reversal is not None:
            raise HTTPException(
                status_code=409,
                detail=(
                    "Historical payment state is not consistent with a missing "
                    "reversal document"
                ),
            )
        cls._require_reconciled_existing_documents(payment)
        source_reference = request.source_reference.strip()
        if len(source_reference) < 3:
            raise HTTPException(
                status_code=400,
                detail="A reviewed external return source reference is required",
            )
        document_id = proposed_return_id or uuid4()
        if (
            get_by_id(db, PaymentRefund, document_id) is not None
            or get_by_id(db, PaymentReversal, document_id) is not None
        ):
            raise HTTPException(
                status_code=409, detail="Proposed return document ID is already used"
            )
        amount = round_money(to_decimal(request.return_amount))
        origin = (
            PaymentRefundOrigin.provider_event
            if payment.provider_id is not None
            else PaymentRefundOrigin.manual
        )
        if return_type == "refund":
            record: PaymentRefund | PaymentReversal = PaymentRefund(
                id=document_id,
                payment_id=payment.id,
                amount=amount,
                currency=payment.currency,
                origin=origin,
                reason=request.reason.strip(),
            )
        else:
            reversal_origin = (
                PaymentReversalOrigin.provider_event
                if payment.provider_id is not None
                else PaymentReversalOrigin.manual
            )
            record = PaymentReversal(
                id=document_id,
                payment_id=payment.id,
                amount=amount,
                currency=payment.currency,
                origin=reversal_origin,
                reason=request.reason.strip(),
            )
        evidence_request = BillingAccountPaymentReturnReconciliationRequest(
            billing_account_ledger_entry_id=request.billing_account_ledger_entry_id,
            allocation_ledger_entry_ids=request.allocation_ledger_entry_ids,
            provider_event_id=request.provider_event_id,
            reason=request.reason,
        )
        evidence_preview = cls._build_preview(
            db,
            payment_id,
            return_type,
            str(document_id),
            evidence_request,
            lock=lock,
            record_override=record,
            record_is_new=True,
        )
        if (
            evidence_preview.payment_state_after
            != evidence_preview.payment_state_before
        ):
            raise HTTPException(
                status_code=409,
                detail=(
                    "Selected return evidence does not explain the historical "
                    "payment state"
                ),
            )
        values = {
            "kind": "historical_consolidated_return_document_reconstruction",
            "proposed_return_id": str(document_id),
            "return_type": return_type,
            "payment_id": str(payment.id),
            "source_reference": source_reference,
            "evidence_fingerprint": evidence_preview.fingerprint,
            "payment_state_before": evidence_preview.payment_state_before.value,
            "payment_state_after": evidence_preview.payment_state_after.value,
            "return_document_created": False,
            "money_posted": False,
            "billing_account_balance_changed": False,
            "service_access_consequence": (
                "none_return_document_reconstruction_no_access_decision"
            ),
        }
        fingerprint = hashlib.sha256(
            json.dumps(values, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest()
        return BillingAccountPaymentReturnDocumentReconstructionPreviewRead(
            proposed_return_id=document_id,
            return_type=return_type,
            payment_id=payment.id,
            billing_account_id=evidence_preview.billing_account_id,
            currency=payment.currency,
            return_amount=evidence_preview.return_amount,
            source_reference=source_reference,
            payment_state_before=evidence_preview.payment_state_before,
            payment_state_after=evidence_preview.payment_state_after,
            payment_refunded_amount_before=(
                evidence_preview.payment_refunded_amount_before
            ),
            payment_refunded_amount_after=(
                evidence_preview.payment_refunded_amount_after
            ),
            recorded_consolidated_credit=(
                evidence_preview.recorded_consolidated_credit
            ),
            evidenced_consolidated_credit=(
                evidence_preview.evidenced_consolidated_credit
            ),
            projection_drift=evidence_preview.projection_drift,
            billing_account_return_amount=(
                evidence_preview.billing_account_return_amount
            ),
            billing_account_ledger_entry_id=(
                evidence_preview.billing_account_ledger_entry_id
            ),
            allocation_return_amount=evidence_preview.allocation_return_amount,
            allocation_evidence=evidence_preview.allocation_evidence,
            provider_event_id=evidence_preview.provider_event_id,
            return_document_created=False,
            money_posted=False,
            billing_account_balance_changed=False,
            service_access_consequence=str(values["service_access_consequence"]),
            evidence_fingerprint=evidence_preview.fingerprint,
            fingerprint=fingerprint,
        )

    @classmethod
    def preview_document_reconstruction(
        cls,
        db: Session,
        payment_id: str,
        return_type: str,
        request: BillingAccountPaymentReturnDocumentReconstructionRequest,
    ) -> BillingAccountPaymentReturnDocumentReconstructionPreviewRead:
        return cls._build_document_reconstruction_preview(
            db,
            payment_id,
            return_type,
            request,
        )

    @classmethod
    def _document_reconstruction_result(
        cls,
        evidence: ConsolidatedPaymentReturnDocumentReconstructionEvidence,
        *,
        replay: bool,
    ) -> BillingAccountPaymentReturnDocumentReconstructionResultRead:
        reconciliation = evidence.reconciliation_evidence
        result = cls._result(reconciliation, replay=replay)
        return BillingAccountPaymentReturnDocumentReconstructionResultRead(
            reconstruction_evidence_id=evidence.id,
            reconciliation_evidence_id=reconciliation.id,
            return_type=result.return_type,
            return_id=result.return_id,
            payment_id=result.payment_id,
            billing_account_id=result.billing_account_id,
            payment_state=result.payment_state,
            return_amount=result.return_amount,
            currency=result.currency,
            source_reference=evidence.source_reference,
            billing_account_ledger_entry_id=(result.billing_account_ledger_entry_id),
            allocation_evidence_ids=result.allocation_evidence_ids,
            subscriber_ledger_entry_ids=result.subscriber_ledger_entry_ids,
            provider_event_id=result.provider_event_id,
            return_document_created=True,
            money_posted=False,
            billing_account_balance_changed=False,
            service_access_consequence=(
                "none_return_document_reconstruction_no_access_decision"
            ),
            idempotent_replay=replay,
        )

    @classmethod
    def _document_reconstruction_replay(
        cls,
        db: Session,
        *,
        key: str,
        fingerprint: str,
        payment_id: str,
        return_type: str,
    ) -> BillingAccountPaymentReturnDocumentReconstructionResultRead | None:
        reservation = (
            db.query(IdempotencyKey)
            .filter(
                IdempotencyKey.scope
                == _RETURN_DOCUMENT_RECONSTRUCTION_IDEMPOTENCY_SCOPE
            )
            .filter(IdempotencyKey.key == key)
            .first()
        )
        if reservation is None:
            return None
        if not reservation.ref_id:
            raise HTTPException(
                status_code=409, detail="Return document reconstruction is in progress"
            )
        evidence = get_by_id(
            db,
            ConsolidatedPaymentReturnDocumentReconstructionEvidence,
            reservation.ref_id,
        )
        if evidence is None or evidence.preview_fingerprint != fingerprint:
            raise HTTPException(
                status_code=409,
                detail="Return document reconstruction replay does not match",
            )
        result = cls._document_reconstruction_result(evidence, replay=True)
        if result.return_type != return_type or str(result.payment_id) != payment_id:
            raise HTTPException(
                status_code=409,
                detail="Idempotency key belongs to another return reconstruction",
            )
        return result

    @classmethod
    def reconstruct_missing_document(
        cls,
        db: Session,
        payment_id: str,
        return_type: str,
        command: BillingAccountPaymentReturnDocumentReconstructionConfirm,
        *,
        actor_type: AuditActorType = AuditActorType.system,
        actor_id: str | None = None,
        commit: bool = True,
    ) -> BillingAccountPaymentReturnDocumentReconstructionResultRead:
        key = _normalize_key(command.idempotency_key)
        replay = cls._document_reconstruction_replay(
            db,
            key=key,
            fingerprint=command.preview_fingerprint,
            payment_id=payment_id,
            return_type=return_type,
        )
        if replay is not None:
            return replay
        request = BillingAccountPaymentReturnDocumentReconstructionRequest(
            billing_account_ledger_entry_id=(command.billing_account_ledger_entry_id),
            allocation_ledger_entry_ids=command.allocation_ledger_entry_ids,
            provider_event_id=command.provider_event_id,
            reason=command.reason,
            return_amount=command.return_amount,
            source_reference=command.source_reference,
        )
        try:
            preview = cls._build_document_reconstruction_preview(
                db,
                payment_id,
                return_type,
                request,
                proposed_return_id=command.proposed_return_id,
                lock=True,
            )
        except HTTPException as exc:
            raise HTTPException(
                status_code=409,
                detail="Financial evidence changed after preview; preview again",
            ) from exc
        if preview.fingerprint != command.preview_fingerprint:
            raise HTTPException(
                status_code=409,
                detail="Financial evidence changed after preview; preview again",
            )
        replay = cls._document_reconstruction_replay(
            db,
            key=key,
            fingerprint=command.preview_fingerprint,
            payment_id=payment_id,
            return_type=return_type,
        )
        if replay is not None:
            return replay
        reservation = IdempotencyKey(
            scope=_RETURN_DOCUMENT_RECONSTRUCTION_IDEMPOTENCY_SCOPE,
            key=key,
        )
        db.add(reservation)
        try:
            payment = lock_for_update(db, Payment, payment_id)
            if payment is None or payment.billing_account_id is None:
                raise HTTPException(
                    status_code=404, detail="Consolidated payment not found"
                )
            if return_type == "refund":
                record: PaymentRefund | PaymentReversal = PaymentRefund(
                    id=preview.proposed_return_id,
                    payment_id=payment.id,
                    provider_event_id=preview.provider_event_id,
                    billing_account_ledger_entry_id=None,
                    amount=preview.return_amount,
                    currency=preview.currency,
                    origin=(
                        PaymentRefundOrigin.provider_event
                        if preview.provider_event_id is not None
                        else PaymentRefundOrigin.manual
                    ),
                    reason=request.reason.strip(),
                    preview_fingerprint=None,
                )
            else:
                record = PaymentReversal(
                    id=preview.proposed_return_id,
                    payment_id=payment.id,
                    provider_event_id=preview.provider_event_id,
                    billing_account_ledger_entry_id=None,
                    amount=preview.return_amount,
                    currency=preview.currency,
                    origin=(
                        PaymentReversalOrigin.provider_event
                        if preview.provider_event_id is not None
                        else PaymentReversalOrigin.manual
                    ),
                    reason=request.reason.strip(),
                    preview_fingerprint=None,
                )
            db.add(record)
            db.flush()
            db.expire(payment, ["refunds", "reversal"])
            internal_key = (
                "return-document-evidence-" + hashlib.sha256(key.encode()).hexdigest()
            )
            reconciliation_result = cls.reconcile_historical_evidence(
                db,
                payment_id,
                return_type,
                str(record.id),
                BillingAccountPaymentReturnReconciliationConfirm(
                    billing_account_ledger_entry_id=(
                        request.billing_account_ledger_entry_id
                    ),
                    allocation_ledger_entry_ids=(request.allocation_ledger_entry_ids),
                    provider_event_id=request.provider_event_id,
                    reason=request.reason,
                    preview_fingerprint=preview.evidence_fingerprint,
                    idempotency_key=internal_key,
                ),
                actor_type=actor_type,
                actor_id=actor_id,
                commit=False,
            )
            reconstruction = ConsolidatedPaymentReturnDocumentReconstructionEvidence(
                reconciliation_evidence_id=(
                    reconciliation_result.reconciliation_evidence_id
                ),
                historical_payment_state=preview.payment_state_before.value,
                source_reference=preview.source_reference,
                preview_fingerprint=preview.fingerprint,
            )
            db.add(reconstruction)
            db.flush()
            AuditEvents.stage(
                db,
                AuditEventCreate(
                    actor_type=actor_type,
                    actor_id=actor_id,
                    action="reconstruct_consolidated_return_document",
                    entity_type=f"payment_{return_type}",
                    entity_id=str(record.id),
                    metadata_={
                        "reconstruction_evidence_id": str(reconstruction.id),
                        "reconciliation_evidence_id": str(
                            reconciliation_result.reconciliation_evidence_id
                        ),
                        "payment_id": str(payment.id),
                        "billing_account_id": str(payment.billing_account_id),
                        "historical_payment_state": (
                            preview.payment_state_before.value
                        ),
                        "return_type": return_type,
                        "return_amount": str(preview.return_amount),
                        "currency": preview.currency,
                        "source_reference": preview.source_reference,
                        "preview_fingerprint": preview.fingerprint,
                        "return_document_created": True,
                        "money_posted": False,
                        "billing_account_balance_changed": False,
                        "service_access_consequence": (
                            preview.service_access_consequence
                        ),
                    },
                ),
            )
            reservation.ref_id = str(reconstruction.id)
            db.flush()
            if commit:
                db.commit()
                db.refresh(reconstruction)
            return cls._document_reconstruction_result(
                reconstruction,
                replay=False,
            )
        except IntegrityError as exc:
            db.rollback()
            replay = cls._document_reconstruction_replay(
                db,
                key=key,
                fingerprint=command.preview_fingerprint,
                payment_id=payment_id,
                return_type=return_type,
            )
            if replay is not None:
                return replay
            raise HTTPException(
                status_code=409,
                detail="Historical return document is already reconstructed",
            ) from exc
        except Exception:
            db.rollback()
            raise

    @staticmethod
    def _result(
        evidence: ConsolidatedPaymentReturnReconciliationEvidence,
        *,
        replay: bool,
    ) -> BillingAccountPaymentReturnReconciliationResultRead:
        if evidence.refund is not None:
            return_type = "refund"
            record: PaymentRefund | PaymentReversal = evidence.refund
        elif evidence.reversal is not None:
            return_type = "reversal"
            record = evidence.reversal
        else:
            raise HTTPException(
                status_code=409, detail="Return reconciliation owner is missing"
            )
        payment = record.payment
        if payment.billing_account_id is None:
            raise HTTPException(
                status_code=409, detail="Return reconciliation payment is incomplete"
            )
        return BillingAccountPaymentReturnReconciliationResultRead(
            reconciliation_evidence_id=evidence.id,
            return_type=return_type,
            return_id=record.id,
            payment_id=payment.id,
            billing_account_id=payment.billing_account_id,
            payment_state=payment.status,
            return_amount=round_money(to_decimal(record.amount)),
            currency=record.currency,
            billing_account_ledger_entry_id=record.billing_account_ledger_entry_id,
            allocation_evidence_ids=[
                item.id for item in record.consolidated_allocation_evidence
            ],
            subscriber_ledger_entry_ids=[
                item.ledger_entry_id for item in record.consolidated_allocation_evidence
            ],
            provider_event_id=record.provider_event_id,
            money_posted=False,
            billing_account_balance_changed=False,
            service_access_consequence=(
                "none_historical_return_evidence_no_access_decision"
            ),
            idempotent_replay=replay,
        )

    @classmethod
    def _replay(
        cls,
        db: Session,
        *,
        key: str,
        fingerprint: str,
        return_type: str,
        return_id: str,
    ) -> BillingAccountPaymentReturnReconciliationResultRead | None:
        reservation = (
            db.query(IdempotencyKey)
            .filter(IdempotencyKey.scope == _RETURN_RECONCILIATION_IDEMPOTENCY_SCOPE)
            .filter(IdempotencyKey.key == key)
            .first()
        )
        if reservation is None:
            return None
        if not reservation.ref_id:
            raise HTTPException(
                status_code=409, detail="Return reconciliation is in progress"
            )
        evidence = get_by_id(
            db, ConsolidatedPaymentReturnReconciliationEvidence, reservation.ref_id
        )
        if evidence is None or evidence.preview_fingerprint != fingerprint:
            raise HTTPException(
                status_code=409,
                detail="Return reconciliation replay evidence does not match",
            )
        result = cls._result(evidence, replay=True)
        if result.return_type != return_type or str(result.return_id) != return_id:
            raise HTTPException(
                status_code=409,
                detail="Idempotency key belongs to a different return",
            )
        return result

    @classmethod
    def reconcile_historical_evidence(
        cls,
        db: Session,
        payment_id: str,
        return_type: str,
        return_id: str,
        command: BillingAccountPaymentReturnReconciliationConfirm,
        *,
        actor_type: AuditActorType = AuditActorType.system,
        actor_id: str | None = None,
        commit: bool = True,
    ) -> BillingAccountPaymentReturnReconciliationResultRead:
        key = _normalize_key(command.idempotency_key)
        replay = cls._replay(
            db,
            key=key,
            fingerprint=command.preview_fingerprint,
            return_type=return_type,
            return_id=return_id,
        )
        if replay is not None:
            return replay
        request = BillingAccountPaymentReturnReconciliationRequest(
            billing_account_ledger_entry_id=(command.billing_account_ledger_entry_id),
            allocation_ledger_entry_ids=command.allocation_ledger_entry_ids,
            provider_event_id=command.provider_event_id,
            reason=command.reason,
        )
        try:
            preview = cls._build_preview(
                db,
                payment_id,
                return_type,
                return_id,
                request,
                lock=True,
            )
        except HTTPException as exc:
            raise HTTPException(
                status_code=409,
                detail="Financial evidence changed after preview; preview again",
            ) from exc
        if preview.fingerprint != command.preview_fingerprint:
            raise HTTPException(
                status_code=409,
                detail="Financial evidence changed after preview; preview again",
            )
        replay = cls._replay(
            db,
            key=key,
            fingerprint=command.preview_fingerprint,
            return_type=return_type,
            return_id=return_id,
        )
        if replay is not None:
            return replay
        reservation = IdempotencyKey(
            scope=_RETURN_RECONCILIATION_IDEMPOTENCY_SCOPE, key=key
        )
        db.add(reservation)
        try:
            payment, record = cls._record(
                db,
                payment_id=payment_id,
                return_type=return_type,
                return_id=return_id,
                lock=True,
            )
            record.billing_account_ledger_entry_id = (
                preview.billing_account_ledger_entry_id
            )
            record.provider_event_id = preview.provider_event_id
            record.preview_fingerprint = preview.fingerprint
            if isinstance(record, PaymentRefund):
                record.origin = (
                    PaymentRefundOrigin.provider_event
                    if preview.provider_event_id is not None
                    else PaymentRefundOrigin.manual
                )
            else:
                record.origin = (
                    PaymentReversalOrigin.provider_event
                    if preview.provider_event_id is not None
                    else PaymentReversalOrigin.manual
                )
            existing_allocations = {
                item.payment_allocation_id: item
                for item in record.consolidated_allocation_evidence
            }
            for effect in preview.allocation_evidence:
                if effect.payment_allocation_id in existing_allocations:
                    continue
                db.add(
                    ConsolidatedPaymentReturnAllocationEvidence(
                        refund_id=(
                            record.id if isinstance(record, PaymentRefund) else None
                        ),
                        reversal_id=(
                            record.id if isinstance(record, PaymentReversal) else None
                        ),
                        payment_allocation_id=effect.payment_allocation_id,
                        ledger_entry_id=effect.ledger_entry_id,
                        amount=effect.amount,
                    )
                )
            payment.refunded_amount = preview.payment_refunded_amount_after
            payment.status = preview.payment_state_after
            evidence = ConsolidatedPaymentReturnReconciliationEvidence(
                refund_id=(record.id if isinstance(record, PaymentRefund) else None),
                reversal_id=(
                    record.id if isinstance(record, PaymentReversal) else None
                ),
                preview_fingerprint=preview.fingerprint,
                reason=request.reason.strip(),
            )
            db.add(evidence)
            db.flush()
            db.expire(record, ["consolidated_allocation_evidence"])
            AuditEvents.stage(
                db,
                AuditEventCreate(
                    actor_type=actor_type,
                    actor_id=actor_id,
                    action="reconcile_consolidated_return_evidence",
                    entity_type=f"payment_{return_type}",
                    entity_id=str(record.id),
                    metadata_={
                        "reconciliation_evidence_id": str(evidence.id),
                        "payment_id": str(payment.id),
                        "billing_account_id": str(payment.billing_account_id),
                        "return_type": return_type,
                        "return_amount": str(record.amount),
                        "currency": record.currency,
                        "preview_fingerprint": preview.fingerprint,
                        "billing_account_ledger_entry_id": (
                            str(record.billing_account_ledger_entry_id)
                            if record.billing_account_ledger_entry_id
                            else None
                        ),
                        "subscriber_ledger_entry_ids": [
                            str(item.ledger_entry_id)
                            for item in record.consolidated_allocation_evidence
                        ],
                        "provider_event_id": (
                            str(record.provider_event_id)
                            if record.provider_event_id
                            else None
                        ),
                        "money_posted": False,
                        "billing_account_balance_changed": False,
                        "service_access_consequence": (
                            preview.service_access_consequence
                        ),
                    },
                ),
            )
            reservation.ref_id = str(evidence.id)
            db.flush()
            if commit:
                db.commit()
                db.refresh(evidence)
            return cls._result(evidence, replay=False)
        except IntegrityError as exc:
            db.rollback()
            replay = cls._replay(
                db,
                key=key,
                fingerprint=command.preview_fingerprint,
                return_type=return_type,
                return_id=return_id,
            )
            if replay is not None:
                return replay
            raise HTTPException(
                status_code=409,
                detail="Consolidated return evidence is already reconciled",
            ) from exc
        except Exception:
            db.rollback()
            raise


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
            if not commit:
                raise
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
            if commit:
                db.rollback()
            raise

    @classmethod
    def stage_provider_event(
        cls,
        db: Session,
        *,
        payment_id: str,
        provider_event_id: UUID,
    ) -> ConsolidatedPaymentRefundResult:
        """Stage a signature-verified consolidated refund without committing."""

        return cls._process_provider_event(
            db,
            payment_id=payment_id,
            provider_event_id=provider_event_id,
            complete_transaction=False,
        )

    @classmethod
    def process_provider_event(
        cls,
        db: Session,
        *,
        payment_id: str,
        provider_event_id: UUID,
        commit: bool = False,
    ) -> ConsolidatedPaymentRefundResult:
        """Legacy wrapper; coordinators use :meth:`stage_provider_event`."""

        return cls._process_provider_event(
            db,
            payment_id=payment_id,
            provider_event_id=provider_event_id,
            complete_transaction=commit,
        )

    @classmethod
    def _process_provider_event(
        cls,
        db: Session,
        *,
        payment_id: str,
        provider_event_id: UUID,
        complete_transaction: bool,
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
            commit=complete_transaction,
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
            if not commit:
                raise
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
            if commit:
                db.rollback()
            raise

    @classmethod
    def stage_provider_event(
        cls,
        db: Session,
        *,
        payment_id: str,
        provider_event_id: UUID,
    ) -> ConsolidatedPaymentReversalResult:
        """Stage a signature-verified consolidated reversal without committing."""

        return cls._process_provider_event(
            db,
            payment_id=payment_id,
            provider_event_id=provider_event_id,
            complete_transaction=False,
        )

    @classmethod
    def process_provider_event(
        cls,
        db: Session,
        *,
        payment_id: str,
        provider_event_id: UUID,
        commit: bool = False,
    ) -> ConsolidatedPaymentReversalResult:
        """Legacy wrapper; coordinators use :meth:`stage_provider_event`."""

        return cls._process_provider_event(
            db,
            payment_id=payment_id,
            provider_event_id=provider_event_id,
            complete_transaction=commit,
        )

    @classmethod
    def _process_provider_event(
        cls,
        db: Session,
        *,
        payment_id: str,
        provider_event_id: UUID,
        complete_transaction: bool,
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
            commit=complete_transaction,
        )


class ConsolidatedCreditAllocations:
    """Owner for moving evidenced reseller credit to subscriber receivables."""

    @staticmethod
    def inspect_reconciliation_evidence(
        db: Session, billing_account_id: str
    ) -> BillingAccountCreditConsumptionEvidenceInspectionRead:
        account = get_by_id(db, BillingAccount, billing_account_id)
        if account is None:
            raise HTTPException(status_code=404, detail="Billing account not found")
        recorded = round_money(to_decimal(account.balance))
        evidenced = _billing_account_evidenced_balance(db, account)
        drift = round_money(recorded - evidenced)
        source_positions = _credit_source_positions(db, account, strict=False)
        source_candidates = [
            BillingAccountCreditConsumptionSourceCandidateRead(
                billing_account_ledger_entry_id=source.entry.id,
                payment_id=source.payment.id,
                amount=round_money(to_decimal(source.entry.amount)),
                linked_consumption=source.linked_consumption,
                returned_amount=source.returned_amount,
                available_amount=source.available,
            )
            for source in source_positions
            if source.available > Decimal("0.00")
        ]

        allocations = (
            db.query(PaymentAllocation)
            .join(Payment, Payment.id == PaymentAllocation.payment_id)
            .filter(Payment.billing_account_id == account.id)
            .filter(Payment.account_id.is_(None))
            .filter(PaymentAllocation.is_active.is_(True))
            .order_by(PaymentAllocation.created_at.asc(), PaymentAllocation.id.asc())
            .all()
        )
        allocation_candidates: list[
            BillingAccountCreditConsumptionAllocationCandidateRead
        ] = []
        for allocation in allocations:
            if (
                db.query(BillingAccountCreditAllocationItem.id)
                .filter(
                    BillingAccountCreditAllocationItem.payment_allocation_id
                    == allocation.id
                )
                .first()
                is not None
            ):
                continue
            payment = get_by_id(db, Payment, allocation.payment_id)
            invoice = get_by_id(db, Invoice, allocation.invoice_id)
            subscriber = (
                get_by_id(db, Subscriber, invoice.account_id)
                if invoice is not None
                else None
            )
            if (
                payment is None
                or invoice is None
                or subscriber is None
                or subscriber.reseller_id != account.reseller_id
                or payment.currency != account.currency
            ):
                continue
            amount = round_money(to_decimal(allocation.amount))
            ledger_entries = (
                db.query(LedgerEntry)
                .filter(LedgerEntry.account_id == invoice.account_id)
                .filter(LedgerEntry.invoice_id == invoice.id)
                .filter(LedgerEntry.payment_id == payment.id)
                .filter(LedgerEntry.entry_type == LedgerEntryType.credit)
                .filter(LedgerEntry.source == LedgerSource.payment)
                .filter(LedgerEntry.currency == account.currency)
                .filter(LedgerEntry.amount == amount)
                .filter(LedgerEntry.is_active.is_(True))
                .order_by(LedgerEntry.created_at.asc(), LedgerEntry.id.asc())
                .all()
            )
            allocation_candidates.append(
                BillingAccountCreditConsumptionAllocationCandidateRead(
                    payment_allocation_id=allocation.id,
                    payment_id=payment.id,
                    payment_has_settlement=payment.settlement is not None,
                    invoice_id=invoice.id,
                    subscriber_id=subscriber.id,
                    amount=amount,
                    subscriber_ledger_entry_ids=[entry.id for entry in ledger_entries],
                )
            )

        debit_candidates: list[BillingAccountCreditConsumptionDebitCandidateRead] = []
        debits = (
            db.query(BillingAccountLedgerEntry)
            .filter(BillingAccountLedgerEntry.billing_account_id == account.id)
            .filter(BillingAccountLedgerEntry.entry_type == LedgerEntryType.debit)
            .filter(
                BillingAccountLedgerEntry.source.in_(
                    (LedgerSource.payment, LedgerSource.other)
                )
            )
            .filter(BillingAccountLedgerEntry.currency == account.currency)
            .filter(BillingAccountLedgerEntry.is_active.is_(True))
            .order_by(
                BillingAccountLedgerEntry.created_at.asc(),
                BillingAccountLedgerEntry.id.asc(),
            )
            .all()
        )
        for entry in debits:
            claimed = (
                db.query(BillingAccountCreditAllocation.id)
                .filter(
                    BillingAccountCreditAllocation.billing_account_ledger_entry_id
                    == entry.id
                )
                .first()
                is not None
                or db.query(PaymentRefund.id)
                .filter(PaymentRefund.billing_account_ledger_entry_id == entry.id)
                .first()
                is not None
                or db.query(PaymentReversal.id)
                .filter(PaymentReversal.billing_account_ledger_entry_id == entry.id)
                .first()
                is not None
            )
            if claimed:
                continue
            debit_candidates.append(
                BillingAccountCreditConsumptionDebitCandidateRead(
                    billing_account_ledger_entry_id=entry.id,
                    payment_id=entry.payment_id,
                    amount=round_money(to_decimal(entry.amount)),
                    source=entry.source,
                    balance_after=round_money(to_decimal(entry.balance_after)),
                )
            )

        return BillingAccountCreditConsumptionEvidenceInspectionRead(
            billing_account_id=account.id,
            currency=account.currency,
            recorded_consolidated_credit=recorded,
            evidenced_consolidated_credit=evidenced,
            projection_drift=drift,
            unbacked_projection_amount=max(Decimal("0.00"), drift),
            missing_debit_projection_amount=max(Decimal("0.00"), -drift),
            source_candidates=source_candidates,
            allocation_candidates=allocation_candidates,
            debit_candidates=debit_candidates,
            service_access_consequence="none_inspection_only_no_access_decision",
        )

    @classmethod
    def _build_reconciliation_preview(
        cls,
        db: Session,
        billing_account_id: str,
        request: BillingAccountCreditConsumptionReconciliationRequest,
        *,
        lock: bool = False,
    ) -> BillingAccountCreditConsumptionReconciliationPreviewRead:
        initial = get_by_id(db, BillingAccount, billing_account_id)
        if initial is None:
            raise HTTPException(status_code=404, detail="Billing account not found")
        account = lock_for_update(db, BillingAccount, initial.id) if lock else initial
        if account is None:
            raise HTTPException(status_code=409, detail="Billing account disappeared")
        if lock:
            (
                db.query(BillingAccountLedgerEntry)
                .filter(BillingAccountLedgerEntry.billing_account_id == account.id)
                .with_for_update()
                .all()
            )

        reason = request.reason.strip()
        if len(reason) < 10:
            raise HTTPException(
                status_code=400, detail="A reviewed reconciliation reason is required"
            )
        selections = sorted(
            request.allocation_evidence,
            key=lambda item: str(item.payment_allocation_id),
        )
        allocation_ids = [item.payment_allocation_id for item in selections]
        subscriber_entry_ids = [item.subscriber_ledger_entry_id for item in selections]
        if len(allocation_ids) != len(set(allocation_ids)):
            raise HTTPException(
                status_code=409, detail="An allocation can be selected only once"
            )
        if len(subscriber_entry_ids) != len(set(subscriber_entry_ids)):
            raise HTTPException(
                status_code=409,
                detail="A subscriber ledger entry cannot prove two allocations",
            )

        source_positions = {
            source.entry.id: source
            for source in _credit_source_positions(db, account, strict=False)
        }
        source_selected: dict[UUID, Decimal] = {}
        carrier_selected: dict[UUID, Decimal] = {}
        subscriber_id: UUID | None = None
        effects: list[BillingAccountCreditConsumptionEffectRead] = []
        total = Decimal("0.00")
        for selection in selections:
            allocation = (
                lock_for_update(db, PaymentAllocation, selection.payment_allocation_id)
                if lock
                else get_by_id(db, PaymentAllocation, selection.payment_allocation_id)
            )
            if allocation is None or not allocation.is_active:
                raise HTTPException(
                    status_code=409,
                    detail="Selected payment allocation is not active",
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
                    detail="Selected allocation already has source-consumption evidence",
                )
            payment = (
                lock_for_update(db, Payment, allocation.payment_id)
                if lock
                else get_by_id(db, Payment, allocation.payment_id)
            )
            invoice = (
                lock_for_update(db, Invoice, allocation.invoice_id)
                if lock
                else get_by_id(db, Invoice, allocation.invoice_id)
            )
            if (
                payment is None
                or invoice is None
                or not payment.is_active
                or payment.billing_account_id != account.id
                or payment.account_id is not None
                or payment.status
                not in {PaymentStatus.succeeded, PaymentStatus.partially_refunded}
                or payment.currency != account.currency
                or payment.settlement is None
            ):
                raise HTTPException(
                    status_code=409,
                    detail=(
                        "Selected allocation carrier lacks exact consolidated "
                        "settlement evidence"
                    ),
                )
            member = get_by_id(db, Subscriber, invoice.account_id)
            if member is None or member.reseller_id != account.reseller_id:
                raise HTTPException(
                    status_code=409,
                    detail="Selected allocation does not belong to this reseller",
                )
            if subscriber_id is None:
                subscriber_id = member.id
            elif subscriber_id != member.id:
                raise HTTPException(
                    status_code=409,
                    detail="One reconciliation may cover only one subscriber",
                )
            amount = round_money(to_decimal(allocation.amount))
            if amount <= Decimal("0.00") or invoice.currency != account.currency:
                raise HTTPException(
                    status_code=409, detail="Selected allocation amount is invalid"
                )
            subscriber_entry = (
                lock_for_update(db, LedgerEntry, selection.subscriber_ledger_entry_id)
                if lock
                else get_by_id(db, LedgerEntry, selection.subscriber_ledger_entry_id)
            )
            if (
                subscriber_entry is None
                or not subscriber_entry.is_active
                or subscriber_entry.account_id != invoice.account_id
                or subscriber_entry.invoice_id != invoice.id
                or subscriber_entry.payment_id != payment.id
                or subscriber_entry.entry_type != LedgerEntryType.credit
                or subscriber_entry.source != LedgerSource.payment
                or subscriber_entry.currency != account.currency
                or round_money(to_decimal(subscriber_entry.amount)) != amount
                or (
                    allocation.ledger_entry_id is not None
                    and allocation.ledger_entry_id != subscriber_entry.id
                )
            ):
                raise HTTPException(
                    status_code=409,
                    detail="Selected subscriber ledger entry is not an exact match",
                )
            entry_claim = (
                db.query(PaymentAllocation.id)
                .filter(PaymentAllocation.ledger_entry_id == subscriber_entry.id)
                .filter(PaymentAllocation.id != allocation.id)
                .first()
            )
            if entry_claim is not None:
                raise HTTPException(
                    status_code=409,
                    detail="Selected subscriber ledger evidence is already claimed",
                )
            source = source_positions.get(
                selection.source_billing_account_ledger_entry_id
            )
            if source is None or source.payment.id != payment.id:
                raise HTTPException(
                    status_code=409,
                    detail=(
                        "Selected source credit is not exact settlement evidence "
                        "for the allocation carrier"
                    ),
                )
            selected_from_source = round_money(
                source_selected.get(source.entry.id, Decimal("0.00")) + amount
            )
            if selected_from_source > source.available:
                raise HTTPException(
                    status_code=409,
                    detail="Selected allocation exceeds the exact source credit",
                )
            source_selected[source.entry.id] = selected_from_source
            carrier_selected[payment.id] = round_money(
                carrier_selected.get(payment.id, Decimal("0.00")) + amount
            )
            total = round_money(total + amount)
            effects.append(
                BillingAccountCreditConsumptionEffectRead(
                    payment_allocation_id=allocation.id,
                    payment_id=payment.id,
                    invoice_id=invoice.id,
                    subscriber_id=member.id,
                    subscriber_ledger_entry_id=subscriber_entry.id,
                    source_billing_account_ledger_entry_id=source.entry.id,
                    amount=amount,
                )
            )

        if subscriber_id is None or total <= Decimal("0.00"):
            raise HTTPException(status_code=409, detail="No allocation was selected")
        for payment_id, selected_amount in carrier_selected.items():
            payment = get_by_id(db, Payment, payment_id)
            if payment is None or selected_amount > _later_allocation_gap(db, payment):
                raise HTTPException(
                    status_code=409,
                    detail=(
                        "Selected allocation is not within the payment's missing "
                        "later-consumption evidence"
                    ),
                )

        recorded = round_money(to_decimal(account.balance))
        evidenced = _billing_account_evidenced_balance(db, account)
        drift_before = round_money(recorded - evidenced)
        selected_debit: BillingAccountLedgerEntry | None = None
        if request.billing_account_debit_ledger_entry_id is not None:
            selected_debit = (
                lock_for_update(
                    db,
                    BillingAccountLedgerEntry,
                    request.billing_account_debit_ledger_entry_id,
                )
                if lock
                else get_by_id(
                    db,
                    BillingAccountLedgerEntry,
                    request.billing_account_debit_ledger_entry_id,
                )
            )
            if (
                selected_debit is None
                or not selected_debit.is_active
                or selected_debit.billing_account_id != account.id
                or selected_debit.entry_type != LedgerEntryType.debit
                or selected_debit.source
                not in {LedgerSource.payment, LedgerSource.other}
                or selected_debit.currency != account.currency
                or round_money(to_decimal(selected_debit.amount)) != total
            ):
                raise HTTPException(
                    status_code=409,
                    detail="Selected billing-account debit is not an exact match",
                )
            claimed = (
                db.query(BillingAccountCreditAllocation.id)
                .filter(
                    BillingAccountCreditAllocation.billing_account_ledger_entry_id
                    == selected_debit.id
                )
                .first()
                is not None
                or db.query(PaymentRefund.id)
                .filter(
                    PaymentRefund.billing_account_ledger_entry_id == selected_debit.id
                )
                .first()
                is not None
                or db.query(PaymentReversal.id)
                .filter(
                    PaymentReversal.billing_account_ledger_entry_id == selected_debit.id
                )
                .first()
                is not None
            )
            if claimed:
                raise HTTPException(
                    status_code=409,
                    detail="Selected billing-account debit is already claimed",
                )
            debit_action = "linked_existing"
            evidenced_after = evidenced
            ledger_created = False
        else:
            if not request.create_missing_billing_account_debit:
                raise HTTPException(
                    status_code=409,
                    detail="An exact billing-account debit action is required",
                )
            missing_debit = max(Decimal("0.00"), -drift_before)
            if missing_debit <= Decimal("0.00") or total > missing_debit:
                raise HTTPException(
                    status_code=409,
                    detail=(
                        "Selected allocations do not fit the exact missing-debit "
                        "projection drift"
                    ),
                )
            debit_action = "created_missing"
            evidenced_after = round_money(evidenced - total)
            ledger_created = True

        drift_after = round_money(recorded - evidenced_after)
        values: dict[str, object] = {
            "kind": "historical_consolidated_credit_consumption_reconciliation",
            "billing_account_id": str(account.id),
            "subscriber_id": str(subscriber_id),
            "currency": account.currency,
            "recorded_consolidated_credit_before": str(recorded),
            "recorded_consolidated_credit_after": str(recorded),
            "evidenced_consolidated_credit_before": str(evidenced),
            "evidenced_consolidated_credit_after": str(evidenced_after),
            "projection_drift_before": str(drift_before),
            "projection_drift_after": str(drift_after),
            "allocation_amount": str(total),
            "allocation_effects": [
                effect.model_dump(mode="json") for effect in effects
            ],
            "billing_account_debit_action": debit_action,
            "billing_account_debit_ledger_entry_id": (
                str(selected_debit.id) if selected_debit is not None else None
            ),
            "billing_account_debit_amount": str(total),
            "billing_account_balance_changed": False,
            "ledger_transaction_created": ledger_created,
            "reason": reason,
            "service_access_consequence": (
                "none_historical_evidence_reconciliation_no_access_decision"
            ),
        }
        fingerprint = hashlib.sha256(
            json.dumps(
                values,
                sort_keys=True,
                separators=(",", ":"),
                default=str,
            ).encode()
        ).hexdigest()
        return BillingAccountCreditConsumptionReconciliationPreviewRead(
            billing_account_id=account.id,
            subscriber_id=subscriber_id,
            currency=account.currency,
            recorded_consolidated_credit_before=recorded,
            recorded_consolidated_credit_after=recorded,
            evidenced_consolidated_credit_before=evidenced,
            evidenced_consolidated_credit_after=evidenced_after,
            projection_drift_before=drift_before,
            projection_drift_after=drift_after,
            allocation_amount=total,
            allocation_effects=effects,
            billing_account_debit_action=debit_action,
            billing_account_debit_ledger_entry_id=(
                selected_debit.id if selected_debit is not None else None
            ),
            billing_account_debit_amount=total,
            billing_account_balance_changed=False,
            ledger_transaction_created=ledger_created,
            service_access_consequence=str(values["service_access_consequence"]),
            fingerprint=fingerprint,
        )

    @classmethod
    def preview_reconciliation(
        cls,
        db: Session,
        billing_account_id: str,
        request: BillingAccountCreditConsumptionReconciliationRequest,
    ) -> BillingAccountCreditConsumptionReconciliationPreviewRead:
        return cls._build_reconciliation_preview(db, billing_account_id, request)

    @staticmethod
    def _reconciliation_result(
        allocation: BillingAccountCreditAllocation, *, replay: bool
    ) -> BillingAccountCreditConsumptionReconciliationResultRead:
        evidence = allocation.reconciliation_evidence
        if evidence is None:
            raise HTTPException(
                status_code=409,
                detail="Consolidated credit reconciliation evidence is incomplete",
            )
        return BillingAccountCreditConsumptionReconciliationResultRead(
            reconciliation_evidence_id=evidence.id,
            allocation_id=allocation.id,
            billing_account_id=allocation.billing_account_id,
            subscriber_id=allocation.subscriber_id,
            amount=allocation.amount,
            currency=allocation.currency,
            billing_account_debit_action=evidence.debit_action,
            billing_account_ledger_entry_id=(
                allocation.billing_account_ledger_entry_id
            ),
            payment_allocation_ids=[
                item.payment_allocation_id for item in allocation.items
            ],
            subscriber_ledger_entry_ids=[
                item.subscriber_ledger_entry_id for item in allocation.items
            ],
            billing_account_balance_changed=False,
            service_access_consequence=(
                "none_historical_evidence_reconciliation_no_access_decision"
            ),
            idempotent_replay=replay,
        )

    @classmethod
    def _reconciliation_replay(
        cls,
        db: Session,
        *,
        key: str,
        fingerprint: str,
    ) -> BillingAccountCreditConsumptionReconciliationResultRead | None:
        reservation = (
            db.query(IdempotencyKey)
            .filter(IdempotencyKey.scope == _CREDIT_RECONCILIATION_IDEMPOTENCY_SCOPE)
            .filter(IdempotencyKey.key == key)
            .first()
        )
        if reservation is None:
            return None
        if not reservation.ref_id:
            raise HTTPException(
                status_code=409,
                detail="Consolidated credit reconciliation is in progress",
            )
        allocation = get_by_id(db, BillingAccountCreditAllocation, reservation.ref_id)
        if allocation is None:
            raise HTTPException(
                status_code=409,
                detail="Consolidated credit reconciliation evidence is incomplete",
            )
        if allocation.preview_fingerprint != fingerprint:
            raise HTTPException(
                status_code=409,
                detail="Idempotency key belongs to a different reconciliation preview",
            )
        return cls._reconciliation_result(allocation, replay=True)

    @classmethod
    def reconcile_historical_consumption(
        cls,
        db: Session,
        billing_account_id: str,
        command: BillingAccountCreditConsumptionReconciliationConfirm,
        *,
        actor_type: AuditActorType = AuditActorType.system,
        actor_id: str | None = None,
        commit: bool = True,
    ) -> BillingAccountCreditConsumptionReconciliationResultRead:
        key = _normalize_key(command.idempotency_key)
        replay = cls._reconciliation_replay(
            db, key=key, fingerprint=command.preview_fingerprint
        )
        if replay is not None:
            return replay
        request = BillingAccountCreditConsumptionReconciliationRequest(
            allocation_evidence=command.allocation_evidence,
            billing_account_debit_ledger_entry_id=(
                command.billing_account_debit_ledger_entry_id
            ),
            create_missing_billing_account_debit=(
                command.create_missing_billing_account_debit
            ),
            reason=command.reason,
        )
        try:
            preview = cls._build_reconciliation_preview(
                db, billing_account_id, request, lock=True
            )
        except HTTPException as exc:
            raise HTTPException(
                status_code=409,
                detail="Financial evidence changed after preview; preview again",
            ) from exc
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
        reservation = IdempotencyKey(
            scope=_CREDIT_RECONCILIATION_IDEMPOTENCY_SCOPE,
            key=key,
        )
        db.add(reservation)
        try:
            account = get_by_id(db, BillingAccount, preview.billing_account_id)
            if account is None:
                raise HTTPException(
                    status_code=409, detail="Billing account disappeared"
                )
            debit_entry = (
                get_by_id(
                    db,
                    BillingAccountLedgerEntry,
                    preview.billing_account_debit_ledger_entry_id,
                )
                if preview.billing_account_debit_ledger_entry_id is not None
                else None
            )
            if debit_entry is None:
                debit_entry = BillingAccountLedgerEntry(
                    billing_account_id=account.id,
                    payment_id=None,
                    entry_type=LedgerEntryType.debit,
                    source=LedgerSource.payment,
                    amount=preview.billing_account_debit_amount,
                    currency=preview.currency,
                    balance_after=round_money(to_decimal(account.balance)),
                    memo=(
                        "Reviewed historical consolidated-credit consumption "
                        "reconciliation"
                    ),
                )
                db.add(debit_entry)
                db.flush()
            allocation = BillingAccountCreditAllocation(
                billing_account_id=account.id,
                subscriber_id=preview.subscriber_id,
                billing_account_ledger_entry_id=debit_entry.id,
                amount=preview.allocation_amount,
                currency=preview.currency,
                preview_fingerprint=preview.fingerprint,
                idempotency_key=key,
            )
            db.add(allocation)
            db.flush()
            for effect in preview.allocation_effects:
                payment_allocation = get_by_id(
                    db, PaymentAllocation, effect.payment_allocation_id
                )
                if payment_allocation is None:
                    raise HTTPException(
                        status_code=409,
                        detail="Selected payment allocation disappeared",
                    )
                if payment_allocation.ledger_entry_id is None:
                    payment_allocation.ledger_entry_id = (
                        effect.subscriber_ledger_entry_id
                    )
                db.add(
                    BillingAccountCreditAllocationItem(
                        allocation_id=allocation.id,
                        source_billing_account_ledger_entry_id=(
                            effect.source_billing_account_ledger_entry_id
                        ),
                        payment_allocation_id=effect.payment_allocation_id,
                        subscriber_ledger_entry_id=effect.subscriber_ledger_entry_id,
                        amount=effect.amount,
                    )
                )
            evidence = ConsolidatedCreditConsumptionReconciliationEvidence(
                allocation_id=allocation.id,
                debit_action=preview.billing_account_debit_action,
                reason=request.reason.strip(),
            )
            db.add(evidence)
            db.flush()
            AuditEvents.stage(
                db,
                AuditEventCreate(
                    actor_type=actor_type,
                    actor_id=actor_id,
                    action="reconcile_consolidated_credit_consumption",
                    entity_type="billing_account_credit_allocation",
                    entity_id=str(allocation.id),
                    metadata_={
                        "reconciliation_evidence_id": str(evidence.id),
                        "billing_account_id": str(account.id),
                        "subscriber_id": str(allocation.subscriber_id),
                        "amount": str(allocation.amount),
                        "currency": allocation.currency,
                        "preview_fingerprint": preview.fingerprint,
                        "billing_account_debit_action": (
                            preview.billing_account_debit_action
                        ),
                        "billing_account_ledger_entry_id": str(debit_entry.id),
                        "payment_allocation_ids": [
                            str(item.payment_allocation_id) for item in allocation.items
                        ],
                        "subscriber_ledger_entry_ids": [
                            str(item.subscriber_ledger_entry_id)
                            for item in allocation.items
                        ],
                        "source_billing_account_ledger_entry_ids": [
                            str(item.source_billing_account_ledger_entry_id)
                            for item in allocation.items
                        ],
                        "billing_account_balance_changed": False,
                        "ledger_transaction_created": (
                            preview.ledger_transaction_created
                        ),
                        "service_access_consequence": (
                            preview.service_access_consequence
                        ),
                    },
                ),
            )
            reservation.ref_id = str(allocation.id)
            db.flush()
            if commit:
                db.commit()
                db.refresh(allocation)
            return cls._reconciliation_result(allocation, replay=False)
        except IntegrityError as exc:
            db.rollback()
            replay = cls._reconciliation_replay(
                db, key=key, fingerprint=command.preview_fingerprint
            )
            if replay is not None:
                return replay
            raise HTTPException(
                status_code=409,
                detail="Consolidated credit reconciliation is already recorded",
            ) from exc
        except Exception:
            db.rollback()
            raise

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
consolidated_payment_return_reconciliations = ConsolidatedPaymentReturnReconciliations()
