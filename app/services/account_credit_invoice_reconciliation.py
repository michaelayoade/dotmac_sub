"""Reviewed repair for a deposit whose credit missed an eligible invoice.

This owner does not record cash.  It binds one existing completed deposit
intent, its succeeded settled payment, and one existing non-service invoice,
then asks the canonical account-credit applicator to create only the missing
allocation and paired ledger evidence.
"""

from __future__ import annotations

import enum
import hashlib
import json
from collections.abc import Mapping
from dataclasses import dataclass
from decimal import Decimal
from typing import NoReturn
from uuid import UUID

from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.models.audit import AuditActorType
from app.models.billing import (
    Invoice,
    InvoiceLine,
    InvoiceStatus,
    Payment,
    PaymentAllocation,
    PaymentSettlement,
    PaymentStatus,
    TopupIntent,
)
from app.models.idempotency import IdempotencyKey
from app.schemas.audit import AuditEventCreate
from app.services.audit import AuditEvents
from app.services.billing._common import get_account_credit_balance, lock_account
from app.services.billing.account_credit import (
    AccountCreditApplicationError,
    AccountCreditApplications,
    eligible_invoices,
)
from app.services.billing.payments import PaymentAllocations
from app.services.common import round_money, to_decimal
from app.services.domain_errors import DomainError
from app.services.events import emit_event
from app.services.events.types import EventType
from app.services.owner_commands import (
    CommandContext,
    OwnerCommandDefinition,
    execute_owner_command,
)

OWNER = "financial.account_credit_invoice_reconciliation"
CONCERN = "reviewed stranded account-credit invoice reconciliation"
RECONCILIATION_SCOPE = "billing:invoice:update"
IDEMPOTENCY_SCOPE = "account-credit-invoice:reconcile"
_MAX_REASON_LENGTH = 500

_RECONCILE_COMMAND = OwnerCommandDefinition(
    owner=OWNER,
    concern=CONCERN,
    name="reconcile_account_credit_invoice",
)


class AccountCreditInvoiceReconciliationDisposition(enum.StrEnum):
    eligible = "eligible"
    already_reconciled = "already_reconciled"
    manual_review = "manual_review"


class AccountCreditInvoiceReconciliationError(DomainError):
    """Transport-neutral fail-closed reconciliation rejection."""


def _error(suffix: str, message: str, **details: object) -> NoReturn:
    raise AccountCreditInvoiceReconciliationError(
        code=f"{OWNER}.{suffix}", message=message, details=details
    )


@dataclass(frozen=True, slots=True)
class AccountCreditInvoiceReconciliationQuery:
    account_id: UUID
    invoice_id: UUID
    payment_id: UUID
    topup_intent_id: UUID
    expected_amount: Decimal
    currency: str = "NGN"


@dataclass(frozen=True, slots=True)
class AccountCreditInvoiceReconciliationPreview:
    disposition: AccountCreditInvoiceReconciliationDisposition
    reason: str
    account_id: UUID
    invoice_id: UUID
    invoice_number: str | None
    payment_id: UUID
    settlement_id: UUID | None
    topup_intent_id: UUID
    currency: str
    expected_amount: Decimal
    invoice_balance: Decimal
    account_credit: Decimal
    payment_available: Decimal
    allocation_id: UUID | None
    fingerprint: str

    @property
    def actionable(self) -> bool:
        return (
            self.disposition is AccountCreditInvoiceReconciliationDisposition.eligible
        )


@dataclass(frozen=True, slots=True)
class ReconcileAccountCreditInvoiceCommand:
    query: AccountCreditInvoiceReconciliationQuery
    expected_preview_fingerprint: str
    permission_granted: bool
    authorized_system_user_id: UUID


@dataclass(frozen=True, slots=True)
class AccountCreditInvoiceReconciliationResult:
    account_id: UUID
    invoice_id: UUID
    payment_id: UUID
    settlement_id: UUID
    topup_intent_id: UUID
    allocation_id: UUID
    invoice_ledger_entry_id: UUID
    credit_consumption_ledger_entry_id: UUID
    amount: Decimal
    currency: str
    preview_fingerprint: str
    replayed: bool


def _fingerprint(payload: Mapping[str, object]) -> str:
    return hashlib.sha256(
        json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str).encode()
    ).hexdigest()


def _normalized_query(
    query: AccountCreditInvoiceReconciliationQuery,
) -> tuple[Decimal, str]:
    amount = round_money(to_decimal(query.expected_amount))
    currency = query.currency.strip().upper()
    if amount <= Decimal("0.00"):
        _error("amount_invalid", "Expected reconciliation amount must be positive.")
    if len(currency) != 3 or not currency.isalpha():
        _error("currency_invalid", "Currency must be a three-letter code.")
    return amount, currency


def _preview(
    db: Session,
    query: AccountCreditInvoiceReconciliationQuery,
    *,
    lock: bool,
) -> AccountCreditInvoiceReconciliationPreview:
    expected, currency = _normalized_query(query)
    invoice_stmt = select(Invoice).where(Invoice.id == query.invoice_id)
    payment_stmt = select(Payment).where(Payment.id == query.payment_id)
    intent_stmt = select(TopupIntent).where(TopupIntent.id == query.topup_intent_id)
    if lock:
        invoice_stmt = invoice_stmt.with_for_update()
        payment_stmt = payment_stmt.with_for_update()
        intent_stmt = intent_stmt.with_for_update()
    invoice = db.scalar(invoice_stmt)
    payment = db.scalar(payment_stmt)
    intent = db.scalar(intent_stmt)
    settlement = (
        db.scalar(
            select(PaymentSettlement).where(
                PaymentSettlement.payment_id == query.payment_id
            )
        )
        if payment is not None
        else None
    )
    allocation = db.scalar(
        select(PaymentAllocation).where(
            PaymentAllocation.payment_id == query.payment_id,
            PaymentAllocation.invoice_id == query.invoice_id,
            PaymentAllocation.is_active.is_(True),
        )
    )
    invoice_balance = round_money(
        to_decimal(invoice.balance_due if invoice is not None else Decimal("0.00"))
    )
    account_credit = round_money(
        get_account_credit_balance(db, str(query.account_id), currency=currency)
    )
    payment_available = (
        round_money(
            PaymentAllocations.available_amount_for_reviewed_document_correction(
                db, str(query.payment_id)
            )
        )
        if payment is not None
        else Decimal("0.00")
    )
    active_line_count = int(
        db.scalar(
            select(func.count(InvoiceLine.id)).where(
                InvoiceLine.invoice_id == query.invoice_id,
                InvoiceLine.is_active.is_(True),
            )
        )
        or 0
    )
    service_line_count = int(
        db.scalar(
            select(func.count(InvoiceLine.id)).where(
                InvoiceLine.invoice_id == query.invoice_id,
                InvoiceLine.is_active.is_(True),
                InvoiceLine.subscription_id.is_not(None),
            )
        )
        or 0
    )
    oldest = eligible_invoices(db, str(query.account_id))

    disposition = AccountCreditInvoiceReconciliationDisposition.manual_review
    reason = "Reviewed evidence is incomplete or ambiguous."
    if (
        allocation is not None
        and invoice is not None
        and invoice.status is InvoiceStatus.paid
        and invoice_balance == Decimal("0.00")
        and round_money(to_decimal(allocation.amount)) == expected
        and allocation.ledger_entry_id is not None
        and allocation.consumption_ledger_entry_id is not None
    ):
        disposition = AccountCreditInvoiceReconciliationDisposition.already_reconciled
        reason = "The exact allocation and paired ledger evidence already exist."
    elif invoice is None or payment is None or intent is None or settlement is None:
        reason = "Invoice, payment, deposit intent, or settlement evidence is missing."
    elif (
        invoice.account_id != query.account_id
        or payment.account_id != query.account_id
        or intent.account_id != query.account_id
    ):
        reason = "Reviewed records do not belong to the same account."
    elif (
        not invoice.is_active
        or invoice.is_proforma
        or invoice.status
        not in {
            InvoiceStatus.issued,
            InvoiceStatus.partially_paid,
            InvoiceStatus.overdue,
        }
        or invoice_balance != expected
    ):
        reason = "Invoice is not an exact active payable receivable."
    elif active_line_count == 0 or service_line_count != 0:
        reason = "Only a non-service invoice with active lines can use this repair."
    elif not oldest or oldest[0].id != invoice.id:
        reason = "Invoice is not the account's oldest eligible debt."
    elif (
        not payment.is_active
        or payment.status is not PaymentStatus.succeeded
        or payment.refunds
        or payment.reversal is not None
        or (payment.currency or "NGN").upper() != currency
    ):
        reason = "Selected payment is not active, succeeded, and unreversed."
    elif (
        intent.completed_payment_id != payment.id
        or intent.status != "completed"
        or intent.purpose != "account_credit_deposit"
        or intent.allocation_policy != "credit_only"
        or intent.credit_application_policy != "pay_eligible_invoices"
        or intent.policy_version != 1
        or round_money(to_decimal(intent.requested_amount)) != expected
        or (
            intent.actual_amount is not None
            and round_money(to_decimal(intent.actual_amount))
            != round_money(to_decimal(payment.amount))
        )
        or round_money(to_decimal(payment.amount) - to_decimal(payment.provider_fee))
        != expected
    ):
        reason = "Deposit intent does not authorize this exact invoice application."
    elif (
        settlement.currency.upper() != currency
        or round_money(to_decimal(settlement.amount)) != expected
        or round_money(to_decimal(settlement.unallocated_amount)) != expected
        or settlement.unallocated_ledger_entry_id is None
    ):
        reason = "Settlement does not carry the exact unallocated credit envelope."
    elif account_credit != expected or payment_available != expected:
        reason = "Available account credit or selected-payment room is not exact."
    else:
        disposition = AccountCreditInvoiceReconciliationDisposition.eligible
        reason = "Existing settled deposit exactly funds the oldest non-service debt."

    payload = {
        "disposition": disposition.value,
        "account_id": query.account_id,
        "invoice_id": query.invoice_id,
        "invoice_number": invoice.invoice_number if invoice is not None else None,
        "invoice_status": invoice.status.value if invoice is not None else None,
        "payment_id": query.payment_id,
        "payment_status": payment.status.value if payment is not None else None,
        "settlement_id": settlement.id if settlement is not None else None,
        "topup_intent_id": query.topup_intent_id,
        "intent_status": intent.status if intent is not None else None,
        "currency": currency,
        "expected_amount": expected,
        "invoice_balance": invoice_balance,
        "account_credit": account_credit,
        "payment_available": payment_available,
        "allocation_id": allocation.id if allocation is not None else None,
        "active_line_count": active_line_count,
        "service_line_count": service_line_count,
        "oldest_eligible_invoice_id": oldest[0].id if oldest else None,
    }
    return AccountCreditInvoiceReconciliationPreview(
        disposition=disposition,
        reason=reason,
        account_id=query.account_id,
        invoice_id=query.invoice_id,
        invoice_number=invoice.invoice_number if invoice is not None else None,
        payment_id=query.payment_id,
        settlement_id=settlement.id if settlement is not None else None,
        topup_intent_id=query.topup_intent_id,
        currency=currency,
        expected_amount=expected,
        invoice_balance=invoice_balance,
        account_credit=account_credit,
        payment_available=payment_available,
        allocation_id=allocation.id if allocation is not None else None,
        fingerprint=_fingerprint(payload),
    )


def preview_account_credit_invoice_reconciliation(
    db: Session, query: AccountCreditInvoiceReconciliationQuery
) -> AccountCreditInvoiceReconciliationPreview:
    """Return a read-only, fingerprinted preview for one evidence chain."""

    return _preview(db, query, lock=False)


def _result_from_allocation(
    db: Session,
    *,
    query: AccountCreditInvoiceReconciliationQuery,
    allocation: PaymentAllocation,
    preview_fingerprint: str,
    replayed: bool,
) -> AccountCreditInvoiceReconciliationResult:
    settlement = db.scalar(
        select(PaymentSettlement).where(
            PaymentSettlement.payment_id == query.payment_id
        )
    )
    invoice = db.get(Invoice, query.invoice_id, populate_existing=True)
    expected, currency = _normalized_query(query)
    if (
        settlement is None
        or invoice is None
        or allocation.payment_id != query.payment_id
        or allocation.invoice_id != query.invoice_id
        or not allocation.is_active
        or round_money(to_decimal(allocation.amount)) != expected
        or allocation.ledger_entry_id is None
        or allocation.consumption_ledger_entry_id is None
        or invoice.status is not InvoiceStatus.paid
        or round_money(to_decimal(invoice.balance_due)) != Decimal("0.00")
    ):
        _error("replay_conflict", "Recorded reconciliation evidence has drifted.")
    return AccountCreditInvoiceReconciliationResult(
        account_id=query.account_id,
        invoice_id=query.invoice_id,
        payment_id=query.payment_id,
        settlement_id=settlement.id,
        topup_intent_id=query.topup_intent_id,
        allocation_id=allocation.id,
        invoice_ledger_entry_id=allocation.ledger_entry_id,
        credit_consumption_ledger_entry_id=allocation.consumption_ledger_entry_id,
        amount=expected,
        currency=currency,
        preview_fingerprint=preview_fingerprint,
        replayed=replayed,
    )


def reconcile_account_credit_invoice(
    db: Session,
    command: ReconcileAccountCreditInvoiceCommand,
    *,
    context: CommandContext,
) -> AccountCreditInvoiceReconciliationResult:
    """Create only the missing allocation/ledger evidence in one transaction."""

    return execute_owner_command(
        db,
        definition=_RECONCILE_COMMAND,
        context=context,
        operation=lambda: _reconcile(db, command=command, context=context),
    )


def _reconcile(
    db: Session,
    *,
    command: ReconcileAccountCreditInvoiceCommand,
    context: CommandContext,
) -> AccountCreditInvoiceReconciliationResult:
    key = (context.idempotency_key or "").strip()
    if len(key) < 16 or len(key) > 120:
        _error(
            "idempotency_key_required",
            "Idempotency key must contain 16-120 characters.",
        )
    reason = context.reason.strip()
    if not reason or len(reason) > _MAX_REASON_LENGTH:
        _error("reason_invalid", "Reason must contain 1-500 characters.")
    if context.scope != RECONCILIATION_SCOPE:
        _error("scope_invalid", "Invoice-update scope is required.")
    if not command.permission_granted:
        _error("permission_denied", "Invoice-update permission is required.")
    if len(command.expected_preview_fingerprint) != 64:
        _error("preview_invalid", "A SHA-256 reconciliation preview is required.")

    lock_account(db, str(command.query.account_id))
    reservation = db.scalar(
        select(IdempotencyKey)
        .where(
            IdempotencyKey.scope == IDEMPOTENCY_SCOPE,
            IdempotencyKey.key == key,
        )
        .with_for_update()
    )
    if reservation is not None:
        if reservation.account_id != command.query.account_id or not reservation.ref_id:
            _error(
                "idempotency_conflict",
                "Key belongs to different reconciliation evidence.",
            )
        try:
            allocation_ref, recorded_fingerprint = reservation.ref_id.split("|", 1)
            allocation_id = UUID(allocation_ref)
        except (ValueError, AttributeError):
            _error(
                "replay_conflict",
                "Idempotency evidence has an invalid reconciliation reference.",
            )
        if recorded_fingerprint != command.expected_preview_fingerprint:
            _error(
                "idempotency_conflict",
                "Key was used with a different reconciliation preview.",
            )
        allocation = db.get(PaymentAllocation, allocation_id)
        if allocation is None:
            _error(
                "replay_conflict", "Idempotency evidence names a missing allocation."
            )
        return _result_from_allocation(
            db,
            query=command.query,
            allocation=allocation,
            preview_fingerprint=command.expected_preview_fingerprint,
            replayed=True,
        )

    current = _preview(db, command.query, lock=True)
    if current.fingerprint != command.expected_preview_fingerprint:
        _error("stale_preview", "Reconciliation evidence changed; preview again.")
    if not current.actionable or current.settlement_id is None:
        _error("not_actionable", current.reason, disposition=current.disposition.value)

    reservation = IdempotencyKey(
        scope=IDEMPOTENCY_SCOPE,
        key=key,
        account_id=command.query.account_id,
    )
    db.add(reservation)
    try:
        db.flush()
    except IntegrityError:
        _error("idempotency_conflict", "Key was concurrently reserved.")

    invoice = db.get(Invoice, command.query.invoice_id)
    if invoice is None:
        _error("invoice_missing", "Reviewed invoice disappeared under lock.")
    try:
        application = (
            AccountCreditApplications.apply_invoice_from_selected_payment_fully(
                db,
                invoice,
                payment_id=command.query.payment_id,
                expected_amount=current.expected_amount,
            )
        )
    except AccountCreditApplicationError as exc:
        _error("application_rejected", str(exc), cause_code=exc.code)
    if len(application.allocation_ids) != 1:
        _error("incomplete_reconciliation", "Exactly one allocation was not produced.")
    allocation = db.get(PaymentAllocation, UUID(application.allocation_ids[0]))
    if allocation is None:
        _error("incomplete_reconciliation", "Allocation evidence is missing.")
    reservation.ref_id = f"{allocation.id}|{current.fingerprint}"

    AuditEvents.stage(
        db,
        AuditEventCreate(
            actor_type=AuditActorType.user,
            actor_id=str(command.authorized_system_user_id),
            action="reconcile_account_credit_invoice",
            entity_type="invoice",
            entity_id=str(command.query.invoice_id),
            request_id=str(context.correlation_id),
            metadata_={
                "account_id": str(command.query.account_id),
                "invoice_id": str(command.query.invoice_id),
                "payment_id": str(command.query.payment_id),
                "settlement_id": str(current.settlement_id),
                "topup_intent_id": str(command.query.topup_intent_id),
                "allocation_id": str(allocation.id),
                "amount": str(current.expected_amount),
                "currency": current.currency,
                "preview_fingerprint": current.fingerprint,
                "command_id": str(context.command_id),
                "command_reason": reason,
            },
        ),
    )
    emit_event(
        db,
        EventType.account_credit_invoice_reconciled,
        {
            "invoice_id": str(command.query.invoice_id),
            "payment_id": str(command.query.payment_id),
            "settlement_id": str(current.settlement_id),
            "topup_intent_id": str(command.query.topup_intent_id),
            "allocation_id": str(allocation.id),
            "amount": str(current.expected_amount),
            "currency": current.currency,
            "preview_fingerprint": current.fingerprint,
        },
        account_id=command.query.account_id,
        invoice_id=command.query.invoice_id,
    )
    db.flush()
    return _result_from_allocation(
        db,
        query=command.query,
        allocation=allocation,
        preview_fingerprint=current.fingerprint,
        replayed=False,
    )
