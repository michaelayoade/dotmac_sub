"""Finance-reviewed return of a legacy over-allocation to account credit.

A Splynx-era payment allocation carries no ledger evidence: it never posted a
paired invoice credit or an account-credit consumption debit. When two such
payments were both allocated to one invoice, the invoice is documented as paid
by more than its total. ``financial.payments``' reviewed reversal refuses these
rows ("Allocation lacks paired ledger evidence") and is limited to void
invoices, so the excess stays stuck on the invoice.

This owner is the sanctioned way to move that excess to account credit. It is
deliberately narrow:

* the allocation must be legacy and active: no ledger links, no preview or
  idempotency evidence, **and** real Splynx/import provenance on its payment
  (``splynx_payment_id`` or ``import_run_id``). Missing ledger fields alone also
  describe Sub-native allocations, so they are not proof; a native allocation
  is refused and needs a separate Finance decision;
* no other active payment on the account may repeat the payment's receipt,
  reference or bank session id (possible duplicate-recorded transfer);
* its payment must be an active, succeeded, unrefunded, unreversed customer
  payment with no settlement row, whose only active allocation is this one;
* the payment's whole amount must already be carried by exactly one active,
  invoice-free ledger credit with no consumption debit, which is the proof that
  the ledger already treats the money as account credit. The correct ledger
  posting for the return is therefore **none**: posting a reversal credit would
  count the same money twice;
* the invoice must be active, paid, and stay exactly paid by its remaining
  allocations and applied credit notes (the excess must equal this allocation),
  and the operator must restate the exact amounts.

Flow: ``preview_legacy_over_allocation_return`` (read-only) then
``return_legacy_over_allocation`` which rechecks under account, invoice,
payment, and allocation locks, requires the identical fingerprint, deactivates
the allocation through the payment owner's flush-only participant, and stages
audit and a domain event in the same transaction. It never changes the invoice
status or balance, the payment, or any ledger entry.
"""

from __future__ import annotations

import hashlib
import json
import logging
import re
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
from enum import StrEnum
from typing import NoReturn
from uuid import UUID, uuid5

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.models.audit import AuditActorType
from app.models.billing import (
    CreditNoteApplication,
    Invoice,
    InvoiceStatus,
    LedgerEntry,
    LedgerEntryType,
    LedgerSource,
    Payment,
    PaymentAllocation,
    PaymentSettlement,
    PaymentStatus,
)
from app.models.event_store import EventStore
from app.schemas.audit import AuditEventCreate
from app.services.audit import AuditEvents
from app.services.billing._common import get_account_credit_balance, lock_account
from app.services.billing.payments import (
    LEGACY_OVER_ALLOCATION_OWNER,
    PaymentAllocations,
    ReviewedLegacyOverAllocationReturn,
)
from app.services.common import round_money, to_decimal
from app.services.domain_errors import DomainError
from app.services.events import EventType, emit_event
from app.services.locking import lock_for_update
from app.services.owner_commands import (
    CommandContext,
    OwnerCommandDefinition,
    execute_owner_command,
)

logger = logging.getLogger(__name__)

OWNER = LEGACY_OVER_ALLOCATION_OWNER
CONCERN = "reviewed legacy payment over-allocation return to account credit"
#: The same grant the existing reviewed payment-allocation reversal requires.
CORRECTION_PERMISSION = "billing:payment:update"
RUNBOOK = "docs/runbooks/LEGACY_OVER_ALLOCATION_RETURN.md"

_COMMAND = OwnerCommandDefinition(
    owner=OWNER,
    concern=CONCERN,
    name="return_legacy_over_allocation",
)
_SCHEMA_VERSION = 1
#: Stable namespace for deterministic correction event identities.
_NAMESPACE = UUID("9d3c5e71-4a82-4f06-b1c9-7e2a6d8f4b13")
_MAX_REASON_LENGTH = 500
_MIN_REASON_LENGTH = 16
_MAX_EVIDENCE_REFERENCE_LENGTH = 200
_HEX = frozenset("0123456789abcdef")
_ZERO = Decimal("0.00")
#: Memo prefix the payment owner gives a native allocation's consumption debit.
_CONSUMPTION_MEMO_MARKER = "account-credit consumption"
#: Receipt / session identifiers hidden in free text: long alphanumeric runs
#: that contain a digit (NIP session ids are 30 digits).
_REFERENCE_TOKEN = re.compile(r"[A-Za-z0-9][A-Za-z0-9_\-]{7,}")


def _reference_tokens(payment: Payment) -> set[str]:
    tokens = {
        value.strip().lower()
        for value in (payment.receipt_number, payment.external_id)
        if value and len(value.strip()) >= 6
    }
    for match in _REFERENCE_TOKEN.findall(payment.memo or ""):
        if any(char.isdigit() for char in match):
            tokens.add(match.lower())
    return tokens


def _possible_duplicate_payment_ids(db: Session, payment: Payment) -> tuple[UUID, ...]:
    """Other active payments on the account that repeat this payment's reference.

    A transfer recorded twice (for example once from the bank statement and once
    from the customer's receipt) shows the same receipt or session id in a
    reference or memo. Returning one of the two allocations would then move
    money that another payment may already account for.
    """
    if payment.account_id is None:
        return ()
    tokens = _reference_tokens(payment)
    if not tokens:
        return ()
    duplicates: list[UUID] = []
    others = db.scalars(
        select(Payment).where(
            Payment.account_id == payment.account_id,
            Payment.id != payment.id,
            Payment.is_active.is_(True),
        )
    ).all()
    for other in others:
        haystack = " ".join(
            value.lower()
            for value in (other.memo, other.receipt_number, other.external_id)
            if value
        )
        if any(token in haystack for token in tokens) or any(
            token in (payment.memo or "").lower() for token in _reference_tokens(other)
        ):
            duplicates.append(other.id)
    return tuple(sorted(duplicates, key=str))


class LegacyOverAllocationError(DomainError):
    """Stable fail-closed error raised by this owner."""


def _error(suffix: str, message: str, **details: object) -> NoReturn:
    raise LegacyOverAllocationError(
        code=f"{OWNER}.{suffix}",
        message=message,
        details=details,
        retryable=False,
    )


class LegacyOverAllocationBlocker(StrEnum):
    allocation_inactive = "allocation_inactive"
    allocation_has_ledger_evidence = "allocation_has_ledger_evidence"
    allocation_has_native_evidence = "allocation_has_native_evidence"
    payment_not_eligible = "payment_not_eligible"
    payment_has_settlement = "payment_has_settlement"
    payment_allocated_elsewhere = "payment_allocated_elsewhere"
    payment_account_mismatch = "payment_account_mismatch"
    currency_mismatch = "currency_mismatch"
    invoice_not_paid = "invoice_not_paid"
    expected_amount_mismatch = "expected_amount_mismatch"
    expected_invoice_total_mismatch = "expected_invoice_total_mismatch"
    expected_remaining_mismatch = "expected_remaining_mismatch"
    invoice_would_not_stay_paid = "invoice_would_not_stay_paid"
    allocation_not_exact_excess = "allocation_not_exact_excess"
    payment_credit_ledger_missing = "payment_credit_ledger_missing"
    payment_ledger_evidence_not_exact = "payment_ledger_evidence_not_exact"
    consumption_debit_present = "consumption_debit_present"
    #: Neither the payment nor the allocation shows Splynx/import provenance,
    #: so the missing ledger evidence does not prove a legacy allocation.
    allocation_not_legacy_provenance = "allocation_not_legacy_provenance"
    #: Another active payment on the account carries the same receipt,
    #: reference or bank session id: the transfer may be recorded twice.
    possible_duplicate_payment_reference = "possible_duplicate_payment_reference"


@dataclass(frozen=True, slots=True)
class LegacyOverAllocationQuery:
    """The operator's exact restatement of the amounts Finance approved."""

    allocation_id: UUID
    #: The allocation being returned to account credit.
    expected_amount: Decimal
    #: The invoice total, which the remaining settlement must equal.
    expected_invoice_total: Decimal
    #: Active allocations excluding this one, plus applied credit notes.
    expected_remaining_settlement: Decimal


@dataclass(frozen=True, slots=True)
class RemainingAllocation:
    allocation_id: UUID
    payment_id: UUID
    amount: Decimal
    has_ledger_evidence: bool


@dataclass(frozen=True, slots=True)
class LedgerCreditEvidence:
    """The ledger fact proving the payment is already account credit."""

    ledger_entry_id: UUID | None
    amount: Decimal | None
    active_payment_entry_count: int
    consumption_debit_count: int


@dataclass(frozen=True, slots=True)
class LegacyOverAllocationPreview:
    query: LegacyOverAllocationQuery
    allocation_id: UUID
    payment_id: UUID
    invoice_id: UUID
    invoice_number: str | None
    account_id: UUID
    currency: str
    allocation_amount: Decimal
    payment_amount: Decimal
    invoice_total: Decimal
    invoice_status: str
    invoice_balance_due: Decimal
    settled_before: Decimal
    settled_after: Decimal
    applied_credit_notes: Decimal
    remaining_allocations: tuple[RemainingAllocation, ...]
    #: Allocation-table view only (payment amount minus active allocations,
    #: before and if the allocation is deactivated). It does not change account
    #: credit: the ledger already carries the whole payment as credit.
    payment_allocation_unallocated_before: Decimal
    payment_allocation_unallocated_after: Decimal
    ledger: LedgerCreditEvidence
    account_credit_before: Decimal
    account_credit_after: Decimal
    ledger_postings: tuple[str, ...]
    blockers: tuple[LegacyOverAllocationBlocker, ...]
    fingerprint: str

    @property
    def actionable(self) -> bool:
        return not self.blockers


@dataclass(frozen=True, slots=True)
class ReturnLegacyOverAllocationCommand:
    """Apply a previewed return; the fingerprint binds it to the preview."""

    query: LegacyOverAllocationQuery
    preview_fingerprint: str
    reason: str
    evidence_reference: str
    evidence_sha256: str
    reviewed_by: UUID
    permission_granted: bool


@dataclass(frozen=True, slots=True)
class LegacyOverAllocationResult:
    correction_id: UUID
    allocation_id: UUID
    payment_id: UUID
    invoice_id: UUID
    account_id: UUID
    amount: Decimal
    currency: str
    account_credit_ledger_entry_id: UUID
    preview_fingerprint: str
    replayed: bool


# ---------------------------------------------------------------------------
# Value helpers
# ---------------------------------------------------------------------------


def _money(value: Decimal | int | float | str | None) -> Decimal:
    return round_money(to_decimal(value))


def _json_default(value: object) -> object:
    if isinstance(value, UUID):
        return str(value)
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, Decimal):
        return f"{value:.2f}"
    if isinstance(value, StrEnum):
        return value.value
    raise TypeError(f"unhashable fingerprint value: {type(value).__name__}")


def _hash(payload: object) -> str:
    encoded = json.dumps(
        payload, sort_keys=True, separators=(",", ":"), default=_json_default
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _query_payload(query: LegacyOverAllocationQuery) -> dict[str, object]:
    return {
        "allocation_id": str(query.allocation_id),
        "expected_amount": f"{_money(query.expected_amount):.2f}",
        "expected_invoice_total": f"{_money(query.expected_invoice_total):.2f}",
        "expected_remaining_settlement": (
            f"{_money(query.expected_remaining_settlement):.2f}"
        ),
    }


# ---------------------------------------------------------------------------
# Preview (read-only query)
# ---------------------------------------------------------------------------


def preview_legacy_over_allocation_return(
    db: Session, query: LegacyOverAllocationQuery
) -> LegacyOverAllocationPreview:
    """Validate one legacy over-allocation return without changing state."""
    allocation = db.get(PaymentAllocation, query.allocation_id)
    if allocation is None:
        _error("allocation_not_found", "The payment allocation was not found.")
    payment = db.get(Payment, allocation.payment_id)
    invoice = db.get(Invoice, allocation.invoice_id)
    if payment is None or invoice is None:
        _error(
            "allocation_evidence_incomplete",
            "The allocation's payment or invoice was not found.",
        )

    blockers: set[LegacyOverAllocationBlocker] = set()
    currency = (payment.currency or "").upper()
    amount = _money(allocation.amount)
    total = _money(invoice.total)

    if not allocation.is_active or allocation.reversed_at is not None:
        blockers.add(LegacyOverAllocationBlocker.allocation_inactive)
    if (
        allocation.ledger_entry_id is not None
        or allocation.consumption_ledger_entry_id is not None
    ):
        blockers.add(LegacyOverAllocationBlocker.allocation_has_ledger_evidence)
    if (
        allocation.preview_fingerprint is not None
        or allocation.idempotency_key is not None
        or allocation.reversal_ledger_entry_id is not None
        or allocation.reversal_consumption_ledger_entry_id is not None
        or allocation.reversal_idempotency_key is not None
    ):
        blockers.add(LegacyOverAllocationBlocker.allocation_has_native_evidence)
    if payment.splynx_payment_id is None and payment.import_run_id is None:
        # Absence of ledger fields alone also matches Sub-native allocations
        # whose posting was never made; only Splynx/import provenance proves a
        # legacy allocation. A native allocation needs its own Finance route.
        blockers.add(LegacyOverAllocationBlocker.allocation_not_legacy_provenance)
    duplicate_payment_ids = _possible_duplicate_payment_ids(db, payment)
    if duplicate_payment_ids:
        blockers.add(LegacyOverAllocationBlocker.possible_duplicate_payment_reference)
    if (
        payment.account_id is None
        or payment.status is not PaymentStatus.succeeded
        or not payment.is_active
        or payment.refunds
        or payment.reversal is not None
        or _money(payment.refunded_amount) > _ZERO
        or payment.reserved_for_purchase_id is not None
    ):
        blockers.add(LegacyOverAllocationBlocker.payment_not_eligible)
    if payment.account_id is not None and payment.account_id != invoice.account_id:
        blockers.add(LegacyOverAllocationBlocker.payment_account_mismatch)
    if currency != (invoice.currency or "").upper():
        blockers.add(LegacyOverAllocationBlocker.currency_mismatch)
    if db.scalar(
        select(PaymentSettlement.id).where(PaymentSettlement.payment_id == payment.id)
    ):
        blockers.add(LegacyOverAllocationBlocker.payment_has_settlement)
    if (
        not invoice.is_active
        or invoice.is_proforma
        or invoice.status is not InvoiceStatus.paid
        or _money(invoice.balance_due) > _ZERO
    ):
        blockers.add(LegacyOverAllocationBlocker.invoice_not_paid)

    payment_active = list(
        db.scalars(
            select(PaymentAllocation).where(
                PaymentAllocation.payment_id == payment.id,
                PaymentAllocation.is_active.is_(True),
                PaymentAllocation.reversed_at.is_(None),
            )
        ).all()
    )
    if any(row.id != allocation.id for row in payment_active):
        blockers.add(LegacyOverAllocationBlocker.payment_allocated_elsewhere)

    invoice_active = list(
        db.scalars(
            select(PaymentAllocation)
            .where(
                PaymentAllocation.invoice_id == invoice.id,
                PaymentAllocation.is_active.is_(True),
                PaymentAllocation.reversed_at.is_(None),
            )
            .order_by(PaymentAllocation.created_at, PaymentAllocation.id)
        ).all()
    )
    allocated_now = _money(sum((row.amount for row in invoice_active), _ZERO))
    credited = _money(
        db.scalar(
            select(func.coalesce(func.sum(CreditNoteApplication.amount), 0)).where(
                CreditNoteApplication.invoice_id == invoice.id
            )
        )
        or 0
    )
    settled_before = _money(allocated_now + credited)
    others = [row for row in invoice_active if row.id != allocation.id]
    remaining_allocations = tuple(
        RemainingAllocation(
            allocation_id=row.id,
            payment_id=row.payment_id,
            amount=_money(row.amount),
            has_ledger_evidence=(
                row.ledger_entry_id is not None
                or row.consumption_ledger_entry_id is not None
            ),
        )
        for row in others
    )
    settled_after = _money(settled_before - amount)

    if amount != _money(query.expected_amount):
        blockers.add(LegacyOverAllocationBlocker.expected_amount_mismatch)
    if total != _money(query.expected_invoice_total):
        blockers.add(LegacyOverAllocationBlocker.expected_invoice_total_mismatch)
    if settled_after != _money(query.expected_remaining_settlement):
        blockers.add(LegacyOverAllocationBlocker.expected_remaining_mismatch)
    if settled_after < total:
        blockers.add(LegacyOverAllocationBlocker.invoice_would_not_stay_paid)
    if settled_before - total != amount:
        # Only an allocation that is exactly the excess may be returned; a
        # partial excess or a deficit needs its own Finance decision.
        blockers.add(LegacyOverAllocationBlocker.allocation_not_exact_excess)

    entries = list(
        db.scalars(
            select(LedgerEntry)
            .where(
                LedgerEntry.payment_id == payment.id,
                LedgerEntry.is_active.is_(True),
            )
            .order_by(LedgerEntry.created_at, LedgerEntry.id)
        ).all()
    )
    credits = [
        entry
        for entry in entries
        if entry.entry_type is LedgerEntryType.credit
        and entry.source is LedgerSource.payment
        and entry.invoice_id is None
        and (entry.currency or "").upper() == currency
        and _money(entry.amount) == _money(payment.amount)
    ]
    consumption = [
        entry
        for entry in entries
        if entry.entry_type is LedgerEntryType.debit
        and _CONSUMPTION_MEMO_MARKER in (entry.memo or "")
    ]
    if not credits:
        blockers.add(LegacyOverAllocationBlocker.payment_credit_ledger_missing)
    elif len(credits) > 1 or len(entries) != 1:
        blockers.add(LegacyOverAllocationBlocker.payment_ledger_evidence_not_exact)
    if consumption:
        blockers.add(LegacyOverAllocationBlocker.consumption_debit_present)
    ledger = LedgerCreditEvidence(
        ledger_entry_id=credits[0].id if len(credits) == 1 else None,
        amount=_money(credits[0].amount) if len(credits) == 1 else None,
        active_payment_entry_count=len(entries),
        consumption_debit_count=len(consumption),
    )

    payment_amount = _money(payment.amount)
    allocated_on_payment = _money(sum((row.amount for row in payment_active), _ZERO))
    unallocated_before = _money(payment_amount - allocated_on_payment)
    unallocated_after = _money(unallocated_before + amount)
    account_credit = (
        _money(
            get_account_credit_balance(db, str(payment.account_id), currency=currency)
        )
        if payment.account_id is not None
        else _ZERO
    )

    ordered_blockers = tuple(sorted(blockers, key=lambda value: value.value))
    fingerprint = _hash(
        {
            "owner": OWNER,
            "schema_version": _SCHEMA_VERSION,
            "query": _query_payload(query),
            "allocation": {
                "id": allocation.id,
                "payment_id": payment.id,
                "invoice_id": invoice.id,
                "amount": amount,
                "is_active": bool(allocation.is_active),
                "created_at": allocation.created_at,
            },
            "payment": {
                "account_id": payment.account_id,
                "amount": payment_amount,
                "currency": currency,
                "status": payment.status.value,
            },
            "invoice": {
                "status": invoice.status.value,
                "total": total,
                "balance_due": _money(invoice.balance_due),
                "account_id": invoice.account_id,
            },
            "settled_before": settled_before,
            "credited": credited,
            "remaining": [
                {"id": row.allocation_id, "amount": row.amount}
                for row in remaining_allocations
            ],
            "ledger": {
                "entry_id": ledger.ledger_entry_id,
                "amount": ledger.amount,
                "payment_entries": ledger.active_payment_entry_count,
                "consumption_debits": ledger.consumption_debit_count,
            },
            "account_credit": account_credit,
            "provenance": {
                "splynx_payment_id": payment.splynx_payment_id,
                "import_run_id": payment.import_run_id,
            },
            "possible_duplicate_payment_ids": duplicate_payment_ids,
            "blockers": list(ordered_blockers),
        }
    )
    return LegacyOverAllocationPreview(
        query=query,
        allocation_id=allocation.id,
        payment_id=payment.id,
        invoice_id=invoice.id,
        invoice_number=invoice.invoice_number,
        account_id=invoice.account_id,
        currency=currency,
        allocation_amount=amount,
        payment_amount=payment_amount,
        invoice_total=total,
        invoice_status=invoice.status.value,
        invoice_balance_due=_money(invoice.balance_due),
        settled_before=settled_before,
        settled_after=settled_after,
        applied_credit_notes=credited,
        remaining_allocations=remaining_allocations,
        payment_allocation_unallocated_before=unallocated_before,
        payment_allocation_unallocated_after=unallocated_after,
        ledger=ledger,
        account_credit_before=account_credit,
        account_credit_after=account_credit,
        ledger_postings=(),
        blockers=ordered_blockers,
        fingerprint=fingerprint,
    )


# ---------------------------------------------------------------------------
# Command
# ---------------------------------------------------------------------------


def _correction_event_id(idempotency_key: str) -> UUID:
    return uuid5(_NAMESPACE, f"{OWNER}:return:{idempotency_key}")


def _stored_event(db: Session, event_id: UUID) -> dict[str, object] | None:
    event = db.execute(
        select(EventStore).where(
            EventStore.event_id == event_id,
            EventStore.event_type
            == EventType.payment_allocation_over_allocation_returned.value,
        )
    ).scalar_one_or_none()
    return dict(event.payload or {}) if event is not None else None


def _require_staff(
    db: Session,
    *,
    context: CommandContext,
    system_user_id: UUID,
    permission_granted: bool,
) -> None:
    from app.models.system_user import SystemUser

    if context.scope != CORRECTION_PERMISSION or not permission_granted:
        _error(
            "permission_denied", f"The {CORRECTION_PERMISSION} permission is required."
        )
    user = db.get(SystemUser, system_user_id)
    if user is None or not user.is_active:
        _error(
            "invalid_actor", "The staff member must be an existing, active system user."
        )


def _validated_evidence(command: ReturnLegacyOverAllocationCommand) -> str:
    reason = command.reason.strip()
    if not _MIN_REASON_LENGTH <= len(reason) <= _MAX_REASON_LENGTH:
        _error(
            "invalid_reason",
            f"A reason of {_MIN_REASON_LENGTH}-{_MAX_REASON_LENGTH} characters "
            "explaining the Finance determination is required.",
        )
    reference = command.evidence_reference.strip()
    digest = command.evidence_sha256.strip().lower()
    if (
        not reference
        or len(reference) > _MAX_EVIDENCE_REFERENCE_LENGTH
        or len(digest) != 64
        or not set(digest) <= _HEX
    ):
        _error(
            "invalid_evidence",
            "An evidence reference and the evidence's 64-hex SHA-256 are required.",
        )
    return digest


def _result_from_event(
    payload: Mapping[str, object], *, replayed: bool
) -> LegacyOverAllocationResult:
    return LegacyOverAllocationResult(
        correction_id=UUID(str(payload["correction_id"])),
        allocation_id=UUID(str(payload["allocation_id"])),
        payment_id=UUID(str(payload["payment_id"])),
        invoice_id=UUID(str(payload["invoice_id"])),
        account_id=UUID(str(payload["account_id"])),
        amount=Decimal(str(payload["amount"])),
        currency=str(payload["currency"]),
        account_credit_ledger_entry_id=UUID(
            str(payload["account_credit_ledger_entry_id"])
        ),
        preview_fingerprint=str(payload["preview_fingerprint"]),
        replayed=replayed,
    )


def _replay(
    db: Session,
    correction_id: UUID,
    *,
    command: ReturnLegacyOverAllocationCommand,
    digest: str,
) -> LegacyOverAllocationResult | None:
    """Return the stored outcome for this key, or refuse a different proposal."""
    stored = _stored_event(db, correction_id)
    if stored is None:
        return None
    if (
        stored.get("query") != _query_payload(command.query)
        or stored.get("preview_fingerprint") != command.preview_fingerprint
        or stored.get("evidence_sha256") != digest
        or stored.get("reviewed_by_system_user_id") != str(command.reviewed_by)
    ):
        _error(
            "idempotency_conflict",
            "This idempotency key already recorded a different return.",
        )
    return _result_from_event(stored, replayed=True)


def _lock_chain(db: Session, query: LegacyOverAllocationQuery) -> None:
    """Lock account, invoice, payment, then every allocation of the invoice."""
    allocation = db.get(PaymentAllocation, query.allocation_id)
    if allocation is None:
        _error("allocation_not_found", "The payment allocation was not found.")
    invoice = db.get(Invoice, allocation.invoice_id)
    if invoice is None:
        _error("allocation_evidence_incomplete", "The invoice was not found.")
    lock_account(db, str(invoice.account_id))
    lock_for_update(db, Invoice, invoice.id)
    lock_for_update(db, Payment, allocation.payment_id)
    db.execute(
        select(PaymentAllocation.id)
        .where(PaymentAllocation.invoice_id == invoice.id)
        .order_by(PaymentAllocation.id)
        .with_for_update()
    ).all()
    # Re-read every locked row so the recomputed preview sees committed state.
    db.expire_all()


def return_legacy_over_allocation(
    db: Session,
    command: ReturnLegacyOverAllocationCommand,
    *,
    context: CommandContext,
) -> LegacyOverAllocationResult:
    """Return one legacy over-allocation to account credit atomically."""
    return execute_owner_command(
        db,
        definition=_COMMAND,
        context=context,
        operation=lambda: _return(db, command=command, context=context),
    )


def _return(
    db: Session,
    *,
    command: ReturnLegacyOverAllocationCommand,
    context: CommandContext,
) -> LegacyOverAllocationResult:
    key = (context.idempotency_key or "").strip()
    if not key:
        _error(
            "missing_idempotency_key",
            "A legacy over-allocation return requires a business idempotency key.",
        )
    _require_staff(
        db,
        context=context,
        system_user_id=command.reviewed_by,
        permission_granted=command.permission_granted,
    )
    digest = _validated_evidence(command)
    correction_id = _correction_event_id(key)

    replay = _replay(db, correction_id, command=command, digest=digest)
    if replay is not None:
        return replay

    _lock_chain(db, command.query)
    # A concurrent confirmation with this key may have committed while this one
    # waited for the locks: converge on its stored outcome instead of reporting
    # the (now returned) allocation as stale.
    replay = _replay(db, correction_id, command=command, digest=digest)
    if replay is not None:
        return replay
    preview = preview_legacy_over_allocation_return(db, command.query)
    if preview.fingerprint != command.preview_fingerprint:
        _error(
            "stale_preview",
            "The allocation evidence changed after preview; preview again.",
            expected_fingerprint=command.preview_fingerprint,
            current_fingerprint=preview.fingerprint,
        )
    if not preview.actionable or preview.ledger.ledger_entry_id is None:
        _error(
            "not_actionable",
            "The previewed return has blockers and cannot be applied.",
            blockers=[value.value for value in preview.blockers],
        )

    try:
        PaymentAllocations.stage_reviewed_legacy_over_allocation_return_for_owner(
            db,
            ReviewedLegacyOverAllocationReturn(
                allocation_id=preview.allocation_id,
                payment_id=preview.payment_id,
                invoice_id=preview.invoice_id,
                expected_amount=preview.allocation_amount,
                reviewed_by=command.reviewed_by,
                preview_fingerprint=preview.fingerprint,
                idempotency_key=key,
                reason=command.reason,
            ),
        )
    except DomainError as exc:
        _error(
            "participant_rejected",
            "The payment owner rejected the reviewed allocation return.",
            participant_error=exc.code,
        )

    now = datetime.now(UTC)
    shared: dict[str, object] = {
        "correction_id": str(correction_id),
        "query": _query_payload(command.query),
        "allocation_id": str(preview.allocation_id),
        "payment_id": str(preview.payment_id),
        "invoice_id": str(preview.invoice_id),
        "invoice_number": preview.invoice_number,
        "account_id": str(preview.account_id),
        "amount": str(preview.allocation_amount),
        "currency": preview.currency,
        "invoice_total": str(preview.invoice_total),
        "settled_before": str(preview.settled_before),
        "settled_after": str(preview.settled_after),
        "account_credit_ledger_entry_id": str(preview.ledger.ledger_entry_id),
        "ledger_postings": [],
        "economic_delta": "0.00",
        "preview_fingerprint": preview.fingerprint,
        "reason": command.reason.strip(),
        "evidence_reference": command.evidence_reference.strip(),
        "evidence_sha256": digest,
        "reviewed_by_system_user_id": str(command.reviewed_by),
    }
    AuditEvents.stage(
        db,
        AuditEventCreate(
            actor_type=AuditActorType.user,
            actor_id=str(command.reviewed_by),
            action="return_legacy_over_allocation_to_account_credit",
            entity_type="payment_allocation",
            entity_id=str(preview.allocation_id),
            metadata_=dict(shared),
        ),
    )
    emit_event(
        db,
        EventType.payment_allocation_over_allocation_returned,
        {
            "schema_version": _SCHEMA_VERSION,
            **shared,
            "returned_at": now.isoformat(),
            "actor": context.actor,
            "command_id": str(context.command_id),
            "idempotency_key": key,
        },
        event_id=correction_id,
        actor=context.actor,
        subscriber_id=preview.account_id,
        account_id=preview.account_id,
        invoice_id=preview.invoice_id,
    )
    db.flush()
    logger.info(
        "payment_allocation_over_allocation_returned: correction=%s allocation=%s",
        correction_id,
        preview.allocation_id,
    )
    return LegacyOverAllocationResult(
        correction_id=correction_id,
        allocation_id=preview.allocation_id,
        payment_id=preview.payment_id,
        invoice_id=preview.invoice_id,
        account_id=preview.account_id,
        amount=preview.allocation_amount,
        currency=preview.currency,
        account_credit_ledger_entry_id=preview.ledger.ledger_entry_id,
        preview_fingerprint=preview.fingerprint,
        replayed=False,
    )


__all__ = [
    "CONCERN",
    "CORRECTION_PERMISSION",
    "OWNER",
    "RUNBOOK",
    "LedgerCreditEvidence",
    "LegacyOverAllocationBlocker",
    "LegacyOverAllocationError",
    "LegacyOverAllocationPreview",
    "LegacyOverAllocationQuery",
    "LegacyOverAllocationResult",
    "RemainingAllocation",
    "ReturnLegacyOverAllocationCommand",
    "preview_legacy_over_allocation_return",
    "return_legacy_over_allocation",
]
