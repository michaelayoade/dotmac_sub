"""Audit and repair prepaid invoices that overlap already-paid coverage."""

from __future__ import annotations

import csv
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any
from uuid import UUID

from sqlalchemy.orm import Session, aliased

from app.models.billing import (
    Invoice,
    InvoiceLine,
    InvoiceStatus,
    LedgerEntry,
    PaymentAllocation,
)
from app.models.catalog import BillingMode, Subscription, SubscriptionStatus
from app.models.collections import DunningActionLog, DunningCase, DunningCaseStatus
from app.models.enforcement_lock import EnforcementLock, EnforcementReason
from app.models.subscriber import Subscriber
from app.services.account_lifecycle import compute_account_status, restore_subscription

COLLECTIBLE_BAD_STATUSES = {
    InvoiceStatus.draft,
    InvoiceStatus.issued,
    InvoiceStatus.partially_paid,
    InvoiceStatus.overdue,
}

REPAIR_METADATA_KEY = "prepaid_overlap_repair"
HOLD_REASON = "prepaid_paid_coverage_overlap"


@dataclass(frozen=True)
class PrepaidOverlapCandidate:
    account_id: str
    account_number: str | None
    account_name: str | None
    subscription_id: str
    subscription_status: str
    bad_invoice_id: str
    bad_invoice_number: str | None
    bad_invoice_status: str
    bad_period_start: str | None
    bad_period_end: str | None
    bad_balance_due: str
    valid_paid_invoice_id: str
    valid_paid_invoice_number: str | None
    paid_period_start: str | None
    paid_period_end: str | None
    corrected_next_billing_at: str | None
    action: str = "pending"
    note: str | None = None


def _as_utc(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    return value if value.tzinfo else value.replace(tzinfo=UTC)


def _iso(value: datetime | None) -> str | None:
    value = _as_utc(value)
    return value.isoformat() if value else None


def _invoice_status_value(status: InvoiceStatus | str | None) -> str | None:
    return status.value if isinstance(status, InvoiceStatus) else status


def _subscription_status_value(status: SubscriptionStatus | str | None) -> str | None:
    return status.value if isinstance(status, SubscriptionStatus) else status


def _metadata_with_hold(
    invoice: Invoice, *, paid_invoice_id: str | None = None, paid_through: str | None = None
) -> dict[str, Any]:
    metadata = dict(invoice.metadata_ or {})
    metadata["reconciliation_hold"] = True
    metadata["reconciliation_hold_reason"] = HOLD_REASON
    repair = dict(metadata.get(REPAIR_METADATA_KEY) or {})
    repair.update(
        {
            "reason": HOLD_REASON,
            "detected_at": datetime.now(UTC).isoformat(),
        }
    )
    if paid_invoice_id:
        repair["valid_paid_invoice_id"] = paid_invoice_id
    if paid_through:
        repair["paid_through"] = paid_through
    metadata[REPAIR_METADATA_KEY] = repair
    return metadata


def invoice_paid_prepaid_overlap(
    db: Session, invoice: Invoice
) -> tuple[Invoice, Subscription] | None:
    """Return paid coverage if ``invoice`` overlaps paid prepaid service.

    This is the enforcement freeze predicate: an invoice that matches this must
    not be marked overdue or drive dunning, even before the full repair command
    has been run.
    """

    if not invoice.is_active:
        return None
    if invoice.status not in COLLECTIBLE_BAD_STATUSES:
        return None
    if invoice.billing_period_start is None or invoice.billing_period_end is None:
        return None

    paid_invoice = aliased(Invoice)
    paid_line = aliased(InvoiceLine)
    bad_line = aliased(InvoiceLine)
    row = (
        db.query(paid_invoice, Subscription)
        .select_from(bad_line)
        .join(Subscription, Subscription.id == bad_line.subscription_id)
        .join(
            paid_line,
            (paid_line.subscription_id == bad_line.subscription_id)
            & (paid_line.is_active.is_(True)),
        )
        .join(paid_invoice, paid_invoice.id == paid_line.invoice_id)
        .filter(bad_line.is_active.is_(True))
        .filter(Subscription.billing_mode == BillingMode.prepaid)
        .filter(paid_invoice.id != invoice.id)
        .filter(paid_invoice.account_id == invoice.account_id)
        .filter(paid_invoice.is_active.is_(True))
        .filter(paid_invoice.status == InvoiceStatus.paid)
        .filter(paid_invoice.balance_due <= Decimal("0.00"))
        .filter(paid_invoice.billing_period_start.isnot(None))
        .filter(paid_invoice.billing_period_end.isnot(None))
        .filter(paid_invoice.billing_period_start < invoice.billing_period_end)
        .filter(paid_invoice.billing_period_end > invoice.billing_period_start)
        .order_by(paid_invoice.billing_period_end.desc())
        .first()
    )
    if row is None:
        return None
    return row


def apply_prepaid_overlap_hold(db: Session, invoice: Invoice) -> bool:
    """Mark a suspected overlap so all enforcement paths ignore it."""

    overlap = invoice_paid_prepaid_overlap(db, invoice)
    if overlap is None:
        return False
    paid_invoice, _subscription = overlap
    invoice.metadata_ = _metadata_with_hold(
        invoice,
        paid_invoice_id=str(paid_invoice.id),
        paid_through=_iso(paid_invoice.billing_period_end),
    )
    return True


def _safe_to_void(invoice: Invoice, allocation_total: Decimal, ledger_count: int) -> bool:
    if invoice.status == InvoiceStatus.paid:
        return False
    if allocation_total != Decimal("0.00") or ledger_count:
        return False
    total = Decimal(str(invoice.total or Decimal("0.00")))
    balance = Decimal(str(invoice.balance_due or Decimal("0.00")))
    return balance >= total


def find_prepaid_overlap_candidates(db: Session) -> list[PrepaidOverlapCandidate]:
    """Find unpaid/dunning-risk invoices covered by an already-paid period."""

    bad_invoice = aliased(Invoice)
    bad_line = aliased(InvoiceLine)
    paid_invoice = aliased(Invoice)
    paid_line = aliased(InvoiceLine)
    rows = (
        db.query(
            bad_invoice,
            bad_line,
            paid_invoice,
            Subscription,
            Subscriber,
        )
        .select_from(bad_invoice)
        .join(bad_line, bad_line.invoice_id == bad_invoice.id)
        .join(Subscription, Subscription.id == bad_line.subscription_id)
        .join(Subscriber, Subscriber.id == bad_invoice.account_id)
        .join(
            paid_line,
            (paid_line.subscription_id == bad_line.subscription_id)
            & (paid_line.is_active.is_(True)),
        )
        .join(paid_invoice, paid_invoice.id == paid_line.invoice_id)
        .filter(bad_invoice.is_active.is_(True))
        .filter(bad_line.is_active.is_(True))
        .filter(bad_invoice.status.in_(COLLECTIBLE_BAD_STATUSES))
        .filter(bad_invoice.billing_period_start.isnot(None))
        .filter(bad_invoice.billing_period_end.isnot(None))
        .filter(Subscription.billing_mode == BillingMode.prepaid)
        .filter(paid_invoice.id != bad_invoice.id)
        .filter(paid_invoice.account_id == bad_invoice.account_id)
        .filter(paid_invoice.is_active.is_(True))
        .filter(paid_invoice.status == InvoiceStatus.paid)
        .filter(paid_invoice.balance_due <= Decimal("0.00"))
        .filter(paid_invoice.billing_period_start.isnot(None))
        .filter(paid_invoice.billing_period_end.isnot(None))
        .filter(paid_invoice.billing_period_start < bad_invoice.billing_period_end)
        .filter(paid_invoice.billing_period_end > bad_invoice.billing_period_start)
        .order_by(
            bad_invoice.account_id.asc(),
            bad_invoice.billing_period_start.asc(),
            paid_invoice.billing_period_end.desc(),
        )
        .all()
    )

    by_key: dict[tuple[UUID, UUID], tuple[Any, ...]] = {}
    for row in rows:
        key = (row[0].id, row[3].id)
        current = by_key.get(key)
        if current is None or _as_utc(row[2].billing_period_end) > _as_utc(
            current[2].billing_period_end
        ):
            by_key[key] = row

    candidates: list[PrepaidOverlapCandidate] = []
    for (
        bad,
        _bad_line,
        paid,
        subscription,
        account,
    ) in by_key.values():
        allocation_total = (
            db.query(PaymentAllocation.amount)
            .filter(PaymentAllocation.invoice_id == bad.id)
            .filter(PaymentAllocation.is_active.is_(True))
            .all()
        )
        allocation_sum = sum(
            (Decimal(str(row[0] or Decimal("0.00"))) for row in allocation_total),
            Decimal("0.00"),
        )
        ledger_count = (
            db.query(LedgerEntry.id).filter(LedgerEntry.invoice_id == bad.id).count()
        )
        action = (
            "void_unpaid_invoice"
            if _safe_to_void(bad, allocation_sum, int(ledger_count))
            else "hold_for_manual_review"
        )
        note = None if action == "void_unpaid_invoice" else "invoice has payment/ledger activity"
        candidates.append(
            PrepaidOverlapCandidate(
                account_id=str(account.id),
                account_number=getattr(account, "account_number", None),
                account_name=account.display_name or account.company_name,
                subscription_id=str(subscription.id),
                subscription_status=_subscription_status_value(subscription.status) or "",
                bad_invoice_id=str(bad.id),
                bad_invoice_number=bad.invoice_number,
                bad_invoice_status=_invoice_status_value(bad.status) or "",
                bad_period_start=_iso(bad.billing_period_start),
                bad_period_end=_iso(bad.billing_period_end),
                bad_balance_due=str(bad.balance_due or Decimal("0.00")),
                valid_paid_invoice_id=str(paid.id),
                valid_paid_invoice_number=paid.invoice_number,
                paid_period_start=_iso(paid.billing_period_start),
                paid_period_end=_iso(paid.billing_period_end),
                corrected_next_billing_at=_iso(paid.billing_period_end),
                action=action,
                note=note,
            )
        )
    return candidates


def _resolve_bad_dunning_cases(
    db: Session, bad_invoice_ids: set[str], *, apply: bool
) -> tuple[int, set[str]]:
    if not bad_invoice_ids:
        return 0, set()
    bad_uuids = {UUID(value) for value in bad_invoice_ids}
    case_ids = {
        row[0]
        for row in db.query(DunningActionLog.case_id)
        .filter(DunningActionLog.invoice_id.in_(bad_uuids))
        .distinct()
        .all()
    }
    resolved_case_ids: set[str] = set()
    for case_id in case_ids:
        case = db.get(DunningCase, case_id)
        if case is None or case.status != DunningCaseStatus.open:
            continue
        logs = (
            db.query(DunningActionLog)
            .filter(DunningActionLog.case_id == case.id)
            .all()
        )
        invoice_ids = {log.invoice_id for log in logs if log.invoice_id is not None}
        if not invoice_ids or not invoice_ids.issubset(bad_uuids):
            continue
        resolved_case_ids.add(str(case.id))
        if apply:
            case.status = DunningCaseStatus.resolved
            case.resolved_at = datetime.now(UTC)
            case.notes = (
                f"{case.notes}\nResolved by prepaid overlap repair"
                if case.notes
                else "Resolved by prepaid overlap repair"
            )
    return len(resolved_case_ids), resolved_case_ids


def _restore_wrongly_suspended_subscriptions(
    db: Session, case_ids: set[str], *, apply: bool
) -> tuple[int, list[str]]:
    restored = 0
    restored_ids: list[str] = []
    if not case_ids:
        return restored, restored_ids
    sources = {f"dunning_case:{case_id}" for case_id in case_ids}
    locks = (
        db.query(EnforcementLock)
        .filter(EnforcementLock.reason == EnforcementReason.overdue)
        .filter(EnforcementLock.source.in_(sources))
        .filter(EnforcementLock.is_active.is_(True))
        .all()
    )
    seen_subscriptions: set[str] = set()
    for lock in locks:
        sub_id = str(lock.subscription_id)
        if sub_id in seen_subscriptions:
            continue
        seen_subscriptions.add(sub_id)
        if not apply:
            restored += 1
            restored_ids.append(sub_id)
            continue
        try:
            if restore_subscription(
                db,
                sub_id,
                trigger="payment",
                resolved_by="prepaid_overlap_repair",
                reason=EnforcementReason.overdue,
                notes="Wrong prepaid overlap invoice was voided/held",
                emit=False,
            ):
                restored += 1
                restored_ids.append(sub_id)
        except ValueError:
            continue
    return restored, restored_ids


def repair_prepaid_overlapping_invoices(
    db: Session, *, apply: bool = False, sync_radius: bool = False
) -> dict[str, Any]:
    """Repair overlapping prepaid invoices.

    Dry-run is the default. In apply mode this:
    - places every candidate on reconciliation hold,
    - voids only safe unpaid bad invoices,
    - advances ``next_billing_at`` to the paid-through date,
    - resolves dunning cases tied only to those invoices,
    - clears overdue locks from those cases and restores services when possible.
    """

    candidates = find_prepaid_overlap_candidates(db)
    bad_invoice_ids = {candidate.bad_invoice_id for candidate in candidates}
    subscription_paid_through: dict[str, datetime] = {}
    voided = 0
    held = 0
    manual_review = 0

    for candidate in candidates:
        paid_through = _as_utc(
            datetime.fromisoformat(candidate.corrected_next_billing_at)
            if candidate.corrected_next_billing_at
            else None
        )
        if paid_through is not None:
            current = subscription_paid_through.get(candidate.subscription_id)
            if current is None or paid_through > current:
                subscription_paid_through[candidate.subscription_id] = paid_through

        if not apply:
            if candidate.action == "void_unpaid_invoice":
                voided += 1
            else:
                manual_review += 1
            continue

        invoice = db.get(Invoice, UUID(candidate.bad_invoice_id))
        if invoice is None:
            continue
        invoice.metadata_ = _metadata_with_hold(
            invoice,
            paid_invoice_id=candidate.valid_paid_invoice_id,
            paid_through=candidate.corrected_next_billing_at,
        )
        held += 1
        if candidate.action == "void_unpaid_invoice":
            invoice.status = InvoiceStatus.void
            invoice.balance_due = Decimal("0.00")
            invoice.due_at = None
            invoice.memo = (
                f"{invoice.memo}\nVoided by prepaid overlap repair"
                if invoice.memo
                else "Voided by prepaid overlap repair"
            )
            voided += 1
        else:
            manual_review += 1

    corrected_anchors = 0
    if apply:
        for subscription_id, paid_through in subscription_paid_through.items():
            subscription = db.get(Subscription, UUID(subscription_id))
            if subscription is None:
                continue
            current = _as_utc(subscription.next_billing_at)
            if current is None or current < paid_through:
                subscription.next_billing_at = paid_through
                corrected_anchors += 1

    resolved_cases, case_ids = _resolve_bad_dunning_cases(
        db, bad_invoice_ids, apply=apply
    )
    restored, restored_subscription_ids = _restore_wrongly_suspended_subscriptions(
        db, case_ids, apply=apply
    )

    if apply:
        account_ids = {candidate.account_id for candidate in candidates}
        for account_id in account_ids:
            compute_account_status(db, account_id)
        db.commit()

        if sync_radius and restored_subscription_ids:
            from app.services.radius import reconcile_subscription_connectivity

            for subscription_id in restored_subscription_ids:
                reconcile_subscription_connectivity(db, subscription_id)

    return {
        "apply": apply,
        "candidates": len(candidates),
        "held": held if apply else len(candidates),
        "voided": voided,
        "manual_review": manual_review,
        "corrected_next_billing_at": corrected_anchors
        if apply
        else len(subscription_paid_through),
        "dunning_cases_resolved": resolved_cases,
        "subscriptions_restored": restored,
        "restored_subscription_ids": restored_subscription_ids,
        "report": [asdict(candidate) for candidate in candidates],
    }


def write_prepaid_overlap_report(
    candidates: list[PrepaidOverlapCandidate], path: str | Path
) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    rows = [asdict(candidate) for candidate in candidates]
    fieldnames = list(PrepaidOverlapCandidate.__dataclass_fields__.keys())
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)
