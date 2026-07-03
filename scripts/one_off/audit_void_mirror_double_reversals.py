"""Audit and remove duplicate Splynx-void reversal contra debits.

The June 23 cleanup posted refund-source debit ledger rows for failed/canceled
payments that Splynx had already voided. For accounts where the Splynx mirror
proves the void was already absorbed into ``subscribers.deposit``, those local
debits are contra entries: they lower the portal balance and appear in the UI
even though the source-of-record value is already gone.

Dry-run is the default. ``--counterfactual`` groups suspect rows by account and
checks the balance identity:

    target = deposit + post-cutover succeeded payments - post-cutover invoices
    eligible iff target - current_available == sum(suspect debits)

Only exact-match accounts can be soft-deleted by ``--soft-delete-eligible
--apply``. Soft-delete means ``ledger_entries.is_active = false``; this removes
the contra debit from balances and customer ledger UI while preserving the row.

Some accounts also had their legitimate opening-balance construction debit
incorrectly soft-deleted by the June 24 phantom-opening cleanup. For those,
``--restore-construction-eligible --apply`` reactivates the exact inactive
opening debit whose amount equals ``sum(suspect debits) - gap`` while also
soft-deleting the contra debit rows. This path is likewise verified against the
same counterfactual target before it reports success.
"""

from __future__ import annotations

import argparse
import csv
import sys
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path

from sqlalchemy import case, func, select, text
from sqlalchemy.orm import Session

from app.db import SessionLocal
from app.models.billing import (
    Invoice,
    InvoiceStatus,
    LedgerCategory,
    LedgerEntry,
    LedgerEntryType,
    LedgerSource,
    Payment,
    PaymentStatus,
)
from app.models.collections import DunningActionLog, DunningCase
from app.models.splynx_transaction import SplynxBillingTransaction
from app.models.subscriber import Subscriber
from app.services.collections import get_available_balance
from app.services.common import round_money

DEAD_PAYMENT_STATUSES = (PaymentStatus.failed, PaymentStatus.canceled)
OPENING_MEMO = "Prepaid opening balance @ cutover"
PHANTOM_REVERSAL_MEMO = (
    "Reversal of phantom prepaid opening balance cutover debit [id={id}]"
)
TOLERANCE = Decimal("0.01")
CUTOVER_AT = datetime(2026, 6, 16, tzinfo=UTC)
DEFAULT_OUTPUT = "scratchpad/void_mirror_double_reversals.csv"
DEFAULT_COUNTERFACTUAL_OUTPUT = (
    "scratchpad/void_mirror_double_reversals_counterfactual.csv"
)

SUSPECT_CLASSES = {"seed_differs_review", "review_no_seed"}


@dataclass(frozen=True)
class Finding:
    entry: LedgerEntry
    classification: str
    subscriber_name: str
    splynx_payment_id: int | None
    deposit: Decimal | None
    mirror_excl_deleted: Decimal | None
    seed_total: Decimal | None
    pre_seed_residue: Decimal | None
    implied_open_ar: Decimal | None
    suggested_credit: Decimal | None


@dataclass(frozen=True)
class AccountReview:
    account_id: uuid.UUID
    subscriber_name: str
    classifications: str
    entries: list[LedgerEntry]
    splynx_payment_ids: str
    suspect_total: Decimal
    deposit: Decimal
    current_available: Decimal
    post_cutover_payments: Decimal
    post_cutover_invoices: Decimal
    target_available: Decimal
    gap: Decimal
    gap_minus_suspect: Decimal
    status: str
    post_adjustment_rows: int
    post_adjustment_net: Decimal
    dunning_events: int
    status_events: int
    event_store_events: int
    construction_entry: LedgerEntry | None
    construction_amount: Decimal


def _money(value: Decimal | int | float | str | None) -> Decimal:
    return round_money(value or Decimal("0"))


def _eq(a: Decimal | None, b: Decimal | None) -> bool:
    if a is None or b is None:
        return False
    return abs(a - b) <= TOLERANCE


def _subscriber_name(subscriber: Subscriber | None) -> str:
    if subscriber is None:
        return ""
    for value in (subscriber.display_name, subscriber.company_name):
        if value:
            return value
    return f"{subscriber.first_name or ''} {subscriber.last_name or ''}".strip()


def _lone_refund_debits(session: Session) -> list[tuple[LedgerEntry, Payment]]:
    rows = session.execute(
        select(LedgerEntry, Payment)
        .join(Payment, Payment.id == LedgerEntry.payment_id)
        .where(
            LedgerEntry.is_active.is_(True),
            LedgerEntry.entry_type == LedgerEntryType.debit,
            LedgerEntry.source == LedgerSource.refund,
            Payment.status.in_(DEAD_PAYMENT_STATUSES),
        )
        .order_by(LedgerEntry.created_at)
    ).all()

    lone: list[tuple[LedgerEntry, Payment]] = []
    for entry, payment in rows:
        active_payment_credit = session.execute(
            select(LedgerEntry.id).where(
                LedgerEntry.is_active.is_(True),
                LedgerEntry.payment_id == entry.payment_id,
                LedgerEntry.entry_type == LedgerEntryType.credit,
                LedgerEntry.source == LedgerSource.payment,
            )
        ).first()
        if active_payment_credit is None:
            lone.append((entry, payment))
    return lone


def _mirror_sum_excl_deleted(session: Session, splynx_customer_id: int) -> Decimal:
    signed = case(
        (
            SplynxBillingTransaction.entry_type == "credit",
            SplynxBillingTransaction.amount,
        ),
        else_=-SplynxBillingTransaction.amount,
    )
    total = session.execute(
        select(func.coalesce(func.sum(signed), 0)).where(
            SplynxBillingTransaction.splynx_customer_id == splynx_customer_id,
            SplynxBillingTransaction.deleted.is_(False),
        )
    ).scalar_one()
    return _money(total)


def _mirror_row_deleted(
    session: Session, splynx_customer_id: int, splynx_payment_id: int
) -> bool | None:
    deleted = session.execute(
        select(SplynxBillingTransaction.deleted).where(
            SplynxBillingTransaction.splynx_customer_id == splynx_customer_id,
            SplynxBillingTransaction.splynx_payment_id == splynx_payment_id,
        )
    ).scalars().all()
    if not deleted:
        return None
    return all(deleted)


def _active_seed(
    session: Session, account_id: uuid.UUID
) -> tuple[Decimal, datetime] | None:
    rows = session.execute(
        select(LedgerEntry.amount, LedgerEntry.created_at).where(
            LedgerEntry.account_id == account_id,
            LedgerEntry.is_active.is_(True),
            LedgerEntry.entry_type == LedgerEntryType.credit,
            LedgerEntry.memo == OPENING_MEMO,
        )
    ).all()
    if not rows:
        return None
    total = _money(sum((amount for amount, _created_at in rows), Decimal("0")))
    return total, min(created_at for _amount, created_at in rows)


def _pre_seed_residue(
    session: Session, account_id: uuid.UUID, seed_created_at: datetime
) -> Decimal:
    signed = case(
        (LedgerEntry.entry_type == LedgerEntryType.credit, LedgerEntry.amount),
        else_=-LedgerEntry.amount,
    )
    total = session.execute(
        select(func.coalesce(func.sum(signed), 0)).where(
            LedgerEntry.account_id == account_id,
            LedgerEntry.is_active.is_(True),
            LedgerEntry.invoice_id.is_(None),
            LedgerEntry.created_at < seed_created_at,
        )
    ).scalar_one()
    return _money(total)


def _classify(session: Session, entry: LedgerEntry, payment: Payment) -> Finding:
    subscriber = session.get(Subscriber, entry.account_id)
    deposit = _money(subscriber.deposit) if subscriber and subscriber.deposit is not None else None
    seed = _active_seed(session, entry.account_id)
    seed_total = seed[0] if seed else None
    pre_seed_residue = (
        _pre_seed_residue(session, entry.account_id, seed[1]) if seed else None
    )
    implied_open_ar = (
        _money(seed_total - deposit + pre_seed_residue)
        if seed_total is not None and deposit is not None and pre_seed_residue is not None
        else None
    )
    mirror_excl: Decimal | None = None
    amount = _money(entry.amount)

    def finding(classification: str, suggested: Decimal | None = None) -> Finding:
        return Finding(
            entry=entry,
            classification=classification,
            subscriber_name=_subscriber_name(subscriber),
            splynx_payment_id=payment.splynx_payment_id,
            deposit=deposit,
            mirror_excl_deleted=mirror_excl,
            seed_total=seed_total,
            pre_seed_residue=pre_seed_residue,
            implied_open_ar=implied_open_ar,
            suggested_credit=suggested,
        )

    if subscriber is None or subscriber.splynx_customer_id is None:
        return finding("review_no_splynx_customer")
    if payment.splynx_payment_id is None:
        return finding("review_no_splynx_payment_id")

    mirror_excl = _mirror_sum_excl_deleted(session, subscriber.splynx_customer_id)
    row_deleted = _mirror_row_deleted(
        session, subscriber.splynx_customer_id, payment.splynx_payment_id
    )
    if row_deleted is None:
        return finding("review_no_mirror_row")
    if not row_deleted:
        return finding("review_mirror_not_deleted")
    if deposit is None:
        return finding("review_no_deposit")

    if _eq(deposit, mirror_excl + amount):
        return finding("justified_keep")
    if not _eq(deposit, mirror_excl):
        return finding("review_deposit_mirror_mismatch")
    if seed_total is not None and _eq(seed_total, deposit):
        return finding("confirmed_double_correction", suggested=amount)
    if seed_total is not None:
        return finding("seed_differs_review", suggested=amount)
    return finding("review_no_seed", suggested=amount)


def _post_cutover_succeeded_payments(session: Session, account_id: uuid.UUID) -> Decimal:
    total = session.execute(
        select(func.coalesce(func.sum(Payment.amount), 0)).where(
            Payment.account_id == account_id,
            Payment.is_active.is_(True),
            Payment.status == PaymentStatus.succeeded,
            func.coalesce(Payment.paid_at, Payment.created_at) >= CUTOVER_AT,
        )
    ).scalar_one()
    return _money(total)


def _post_cutover_invoice_totals(session: Session, account_id: uuid.UUID) -> Decimal:
    total = session.execute(
        select(func.coalesce(func.sum(Invoice.total), 0)).where(
            Invoice.account_id == account_id,
            Invoice.is_active.is_(True),
            Invoice.status != InvoiceStatus.void,
            Invoice.is_proforma.is_(False),
            Invoice.created_at >= CUTOVER_AT,
        )
    ).scalar_one()
    return _money(total)


def _post_adjustment_warning(session: Session, account_id: uuid.UUID) -> tuple[int, Decimal]:
    signed = case(
        (LedgerEntry.entry_type == LedgerEntryType.credit, LedgerEntry.amount),
        else_=-LedgerEntry.amount,
    )
    row = session.execute(
        select(func.count(LedgerEntry.id), func.coalesce(func.sum(signed), 0)).where(
            LedgerEntry.account_id == account_id,
            LedgerEntry.is_active.is_(True),
            LedgerEntry.invoice_id.is_(None),
            LedgerEntry.source == LedgerSource.adjustment,
            LedgerEntry.memo != OPENING_MEMO,
            LedgerEntry.created_at >= CUTOVER_AT,
        )
    ).one()
    return int(row[0] or 0), _money(row[1])


def _dunning_event_count(
    session: Session, account_id: uuid.UUID, since: datetime
) -> int:
    return int(
        session.execute(
            select(func.count(DunningActionLog.id))
            .join(DunningCase, DunningCase.id == DunningActionLog.case_id)
            .where(
                DunningCase.account_id == account_id,
                DunningActionLog.executed_at >= since,
            )
        ).scalar_one()
        or 0
    )


def _status_event_count(
    session: Session, account_id: uuid.UUID, since: datetime
) -> int:
    # This table has no ORM model in the app; keep it read-only and narrow.
    return int(
        session.execute(
            text(
                """
                select count(*)
                from subscriber_status_history
                where subscriber_id = :account_id and created_at >= :since
                """
            ),
            {"account_id": str(account_id), "since": since},
        ).scalar_one()
        or 0
    )


def _event_store_count(
    session: Session, account_id: uuid.UUID, since: datetime
) -> int:
    return int(
        session.execute(
            text(
                """
                select count(*)
                from event_store
                where account_id = :account_id
                  and created_at >= :since
                  and event_type in (
                    'dunning.started',
                    'dunning.action_executed',
                    'subscriber.suspended',
                    'subscription.suspended',
                    'subscriber.reactivated',
                    'subscription.resumed',
                    'enforcement_lock.created',
                    'enforcement_lock.resolved'
                  )
                """
            ),
            {"account_id": str(account_id), "since": since},
        ).scalar_one()
        or 0
    )


def _matching_inactive_construction_debit(
    session: Session, account_id: uuid.UUID, amount: Decimal
) -> LedgerEntry | None:
    if amount <= 0:
        return None
    rows = session.execute(
        select(LedgerEntry)
        .where(
            LedgerEntry.account_id == account_id,
            LedgerEntry.is_active.is_(False),
            LedgerEntry.entry_type == LedgerEntryType.debit,
            LedgerEntry.source == LedgerSource.adjustment,
            LedgerEntry.memo == OPENING_MEMO,
            LedgerEntry.invoice_id.is_(None),
            LedgerEntry.payment_id.is_(None),
            LedgerEntry.amount >= amount - TOLERANCE,
            LedgerEntry.amount <= amount + TOLERANCE,
        )
        .order_by(LedgerEntry.created_at.desc())
    ).scalars().all()
    reversed_rows: list[LedgerEntry] = []
    for row in rows:
        reversal_count = session.execute(
            select(func.count(LedgerEntry.id)).where(
                LedgerEntry.account_id == account_id,
                LedgerEntry.is_active.is_(False),
                LedgerEntry.entry_type == LedgerEntryType.credit,
                LedgerEntry.source == LedgerSource.adjustment,
                LedgerEntry.memo == PHANTOM_REVERSAL_MEMO.format(id=row.id),
                LedgerEntry.invoice_id.is_(None),
                LedgerEntry.payment_id.is_(None),
                LedgerEntry.amount >= amount - TOLERANCE,
                LedgerEntry.amount <= amount + TOLERANCE,
            )
        ).scalar_one()
        if reversal_count == 1:
            reversed_rows.append(row)
    if len(reversed_rows) != 1:
        return None
    return reversed_rows[0]


def _counterfactual_reviews(
    session: Session, findings: list[Finding]
) -> list[AccountReview]:
    grouped: dict[uuid.UUID, list[Finding]] = {}
    for finding in findings:
        if finding.classification in SUSPECT_CLASSES:
            grouped.setdefault(finding.entry.account_id, []).append(finding)

    reviews: list[AccountReview] = []
    for account_id, rows in grouped.items():
        subscriber = session.get(Subscriber, account_id)
        deposit = _money(subscriber.deposit if subscriber else None)
        suspect_total = _money(sum((row.entry.amount for row in rows), Decimal("0")))
        post_payments = _post_cutover_succeeded_payments(session, account_id)
        post_invoices = _post_cutover_invoice_totals(session, account_id)
        target = _money(deposit + post_payments - post_invoices)
        current = _money(get_available_balance(session, str(account_id)))
        gap = _money(target - current)
        gap_minus_suspect = _money(gap - suspect_total)
        construction_amount = _money(suspect_total - gap)
        construction_entry = _matching_inactive_construction_debit(
            session, account_id, construction_amount
        )
        if abs(gap_minus_suspect) <= TOLERANCE:
            status = "eligible_exact"
        elif construction_entry is not None:
            status = "eligible_restore_construction"
        else:
            status = "manual_review"
        first_debit_at = min(row.entry.created_at for row in rows)
        adjustment_rows, adjustment_net = _post_adjustment_warning(session, account_id)
        reviews.append(
            AccountReview(
                account_id=account_id,
                subscriber_name=_subscriber_name(subscriber),
                classifications=", ".join(sorted({row.classification for row in rows})),
                entries=[row.entry for row in rows],
                splynx_payment_ids=", ".join(
                    str(row.splynx_payment_id or "") for row in rows
                ),
                suspect_total=suspect_total,
                deposit=deposit,
                current_available=current,
                post_cutover_payments=post_payments,
                post_cutover_invoices=post_invoices,
                target_available=target,
                gap=gap,
                gap_minus_suspect=gap_minus_suspect,
                status=status,
                post_adjustment_rows=adjustment_rows,
                post_adjustment_net=adjustment_net,
                dunning_events=_dunning_event_count(session, account_id, first_debit_at),
                status_events=_status_event_count(session, account_id, first_debit_at),
                event_store_events=_event_store_count(session, account_id, first_debit_at),
                construction_entry=construction_entry,
                construction_amount=construction_amount,
            )
        )
    return sorted(
        reviews,
        key=lambda row: (
            row.status != "eligible_exact",
            -row.suspect_total,
            row.subscriber_name,
        ),
    )


def _write_findings_csv(path: Path, findings: list[Finding]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="") as handle:
        writer = csv.writer(handle)
        writer.writerow(
            [
                "classification",
                "ledger_entry_id",
                "account_id",
                "subscriber_name",
                "debit_amount",
                "payment_id",
                "splynx_payment_id",
                "deposit",
                "mirror_sum_excl_deleted",
                "active_seed_total",
                "pre_seed_residue",
                "implied_open_ar",
                "suggested_credit",
                "entry_created_at",
                "memo",
            ]
        )
        for finding in findings:
            writer.writerow(
                [
                    finding.classification,
                    finding.entry.id,
                    finding.entry.account_id,
                    finding.subscriber_name,
                    finding.entry.amount,
                    finding.entry.payment_id,
                    finding.splynx_payment_id or "",
                    finding.deposit if finding.deposit is not None else "",
                    finding.mirror_excl_deleted
                    if finding.mirror_excl_deleted is not None
                    else "",
                    finding.seed_total if finding.seed_total is not None else "",
                    finding.pre_seed_residue
                    if finding.pre_seed_residue is not None
                    else "",
                    finding.implied_open_ar
                    if finding.implied_open_ar is not None
                    else "",
                    finding.suggested_credit
                    if finding.suggested_credit is not None
                    else "",
                    finding.entry.created_at,
                    finding.entry.memo,
                ]
            )


def _write_counterfactual_csv(path: Path, reviews: list[AccountReview]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="") as handle:
        writer = csv.writer(handle)
        writer.writerow(
            [
                "status",
                "account_id",
                "subscriber_name",
                "classifications",
                "suspect_rows",
                "suspect_debits",
                "deposit",
                "post_cutover_succeeded_payments",
                "post_cutover_invoice_totals",
                "target_available",
                "current_available",
                "gap",
                "gap_minus_suspect",
                "post_adjustment_rows",
                "post_adjustment_net",
                "dunning_events_after_first_debit",
                "status_events_after_first_debit",
                "event_store_events_after_first_debit",
                "construction_amount_to_restore",
                "construction_entry_id",
                "ledger_entry_ids",
                "splynx_payment_ids",
            ]
        )
        for review in reviews:
            writer.writerow(
                [
                    review.status,
                    review.account_id,
                    review.subscriber_name,
                    review.classifications,
                    len(review.entries),
                    review.suspect_total,
                    review.deposit,
                    review.post_cutover_payments,
                    review.post_cutover_invoices,
                    review.target_available,
                    review.current_available,
                    review.gap,
                    review.gap_minus_suspect,
                    review.post_adjustment_rows,
                    review.post_adjustment_net,
                    review.dunning_events,
                    review.status_events,
                    review.event_store_events,
                    review.construction_amount,
                    review.construction_entry.id if review.construction_entry else "",
                    ", ".join(str(entry.id) for entry in review.entries),
                    review.splynx_payment_ids,
                ]
            )


def _soft_delete_eligible(
    session: Session, reviews: list[AccountReview], apply: bool
) -> int:
    eligible = [review for review in reviews if review.status == "eligible_exact"]
    entries = [entry for review in eligible for entry in review.entries if entry.is_active]
    total = _money(sum((entry.amount for entry in entries), Decimal("0")))
    print(
        f"{'APPLY' if apply else 'DRY-RUN'}: soft-delete "
        f"{len(entries)} eligible contra debit rows totaling {total}"
    )
    for review in eligible:
        expected_after = _money(review.current_available + review.suspect_total)
        print(
            f"  {review.subscriber_name}: {review.current_available} -> "
            f"{expected_after} (target {review.target_available})"
        )
    if not apply:
        return 0
    for entry in entries:
        entry.is_active = False
    session.commit()

    failures = []
    for review in eligible:
        actual = _money(get_available_balance(session, str(review.account_id)))
        if not _eq(actual, review.target_available):
            failures.append((review.subscriber_name, actual, review.target_available))
    if failures:
        for name, actual, target in failures:
            print(f"VERIFY FAILED: {name}: available {actual}, expected {target}")
        return 1
    print(f"soft-deleted {len(entries)} contra debit rows")
    return 0


def _restore_construction_eligible(
    session: Session, reviews: list[AccountReview], apply: bool
) -> int:
    eligible = [
        review
        for review in reviews
        if review.status == "eligible_restore_construction"
        and review.construction_entry is not None
    ]
    contra_entries = [
        entry for review in eligible for entry in review.entries if entry.is_active
    ]
    construction_entries = [
        review.construction_entry
        for review in eligible
        if review.construction_entry is not None and not review.construction_entry.is_active
    ]
    contra_total = _money(sum((entry.amount for entry in contra_entries), Decimal("0")))
    construction_total = _money(
        sum((entry.amount for entry in construction_entries), Decimal("0"))
    )
    print(
        f"{'APPLY' if apply else 'DRY-RUN'}: restore "
        f"{len(construction_entries)} construction debit rows totaling "
        f"{construction_total}; soft-delete {len(contra_entries)} contra debit "
        f"rows totaling {contra_total}"
    )
    for review in eligible:
        expected_after = _money(
            review.current_available
            + review.suspect_total
            - review.construction_amount
        )
        print(
            f"  {review.subscriber_name}: {review.current_available} -> "
            f"{expected_after} (target {review.target_available}; restore "
            f"{review.construction_amount})"
        )
    if not apply:
        return 0
    for entry in construction_entries:
        entry.is_active = True
    for entry in contra_entries:
        entry.is_active = False
    session.commit()

    failures = []
    for review in eligible:
        actual = _money(get_available_balance(session, str(review.account_id)))
        if not _eq(actual, review.target_available):
            failures.append((review.subscriber_name, actual, review.target_available))
    if failures:
        for name, actual, target in failures:
            print(f"VERIFY FAILED: {name}: available {actual}, expected {target}")
        return 1
    print(
        f"restored {len(construction_entries)} construction debit rows and "
        f"soft-deleted {len(contra_entries)} contra debit rows"
    )
    return 0


def _soft_delete_entry(session: Session, entry_id: uuid.UUID, apply: bool) -> int:
    entry = session.get(LedgerEntry, entry_id)
    if entry is None:
        print(f"entry {entry_id} not found")
        return 1
    if not entry.is_active:
        print(f"entry {entry_id} is already inactive")
        return 1
    if entry.entry_type is not LedgerEntryType.debit:
        print(f"entry {entry_id} is not a debit")
        return 1
    if entry.source is not LedgerSource.refund:
        print(f"entry {entry_id} is not a refund-source contra debit")
        return 1

    before = _money(get_available_balance(session, str(entry.account_id)))
    expected_after = _money(before + entry.amount)
    print(
        f"{'APPLY' if apply else 'DRY-RUN'}: soft-delete entry {entry.id} "
        f"for account {entry.account_id}; {before} -> {expected_after}"
    )
    if not apply:
        return 0

    entry.is_active = False
    session.commit()
    actual = _money(get_available_balance(session, str(entry.account_id)))
    if not _eq(actual, expected_after):
        print(f"VERIFY FAILED: available {actual}, expected {expected_after}")
        return 1
    print(f"soft-deleted entry {entry.id}; available is now {actual}")
    return 0


def _post_seed_credit(
    session: Session,
    account_id: uuid.UUID,
    amount: Decimal,
    expected_after: Decimal | None,
    apply: bool,
) -> int:
    amount = _money(amount)
    if amount <= 0:
        print("--seed-credit amount must be positive")
        return 1

    existing = session.execute(
        select(LedgerEntry).where(
            LedgerEntry.account_id == account_id,
            LedgerEntry.is_active.is_(True),
            LedgerEntry.entry_type == LedgerEntryType.credit,
            LedgerEntry.source == LedgerSource.adjustment,
            LedgerEntry.category == LedgerCategory.deposit,
            LedgerEntry.memo == OPENING_MEMO,
            LedgerEntry.invoice_id.is_(None),
            LedgerEntry.payment_id.is_(None),
            LedgerEntry.amount >= amount - TOLERANCE,
            LedgerEntry.amount <= amount + TOLERANCE,
        )
    ).scalars().all()
    before = _money(get_available_balance(session, str(account_id)))
    if len(existing) > 1:
        print(
            f"account {account_id} has {len(existing)} matching active seed credits; "
            "refusing to guess"
        )
        return 1
    if len(existing) == 1:
        print(
            f"account {account_id} already has seed credit {existing[0].id}; "
            f"available is {before}"
        )
        if expected_after is not None and not _eq(before, expected_after):
            print(f"VERIFY FAILED: available {before}, expected {expected_after}")
            return 1
        return 0

    after = _money(before + amount)
    print(
        f"{'APPLY' if apply else 'DRY-RUN'}: post seed credit {amount} "
        f"for account {account_id}; {before} -> {after}"
    )
    if not apply:
        return 0

    session.add(
        LedgerEntry(
            account_id=account_id,
            invoice_id=None,
            payment_id=None,
            entry_type=LedgerEntryType.credit,
            source=LedgerSource.adjustment,
            category=LedgerCategory.deposit,
            amount=amount,
            currency="NGN",
            memo=OPENING_MEMO,
        )
    )
    session.commit()
    actual = _money(get_available_balance(session, str(account_id)))
    if not _eq(actual, after):
        print(f"VERIFY FAILED: available {actual}, expected {after}")
        return 1
    if expected_after is not None and not _eq(actual, expected_after):
        print(f"VERIFY FAILED: available {actual}, expected {expected_after}")
        return 1
    print(f"posted seed credit; available is now {actual}")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply", action="store_true", help="write changes")
    parser.add_argument("--csv", type=Path, default=Path(DEFAULT_OUTPUT))
    parser.add_argument(
        "--counterfactual",
        action="store_true",
        help="write account-level target/current/gap review CSV",
    )
    parser.add_argument(
        "--counterfactual-csv",
        type=Path,
        default=Path(DEFAULT_COUNTERFACTUAL_OUTPUT),
    )
    parser.add_argument(
        "--soft-delete-eligible",
        action="store_true",
        help="soft-delete only counterfactual exact-match contra debit rows",
    )
    parser.add_argument(
        "--restore-construction-eligible",
        action="store_true",
        help=(
            "restore exact inactive opening construction debits and soft-delete "
            "matching contra debit rows"
        ),
    )
    parser.add_argument(
        "--soft-delete-entry",
        type=uuid.UUID,
        default=None,
        help="soft-delete one active refund-source debit by ledger entry id",
    )
    parser.add_argument(
        "--seed-credit-account",
        type=uuid.UUID,
        default=None,
        help="post one idempotent opening-balance seed credit to an account",
    )
    parser.add_argument(
        "--seed-credit",
        type=Decimal,
        default=None,
        help="amount for --seed-credit-account",
    )
    parser.add_argument(
        "--expected-after",
        type=Decimal,
        default=None,
        help="expected available balance after a targeted correction",
    )
    args = parser.parse_args()

    session = SessionLocal()
    try:
        if args.soft_delete_entry is not None:
            return _soft_delete_entry(session, args.soft_delete_entry, args.apply)
        if args.seed_credit_account is not None:
            if args.seed_credit is None:
                print("--seed-credit is required with --seed-credit-account")
                return 1
            return _post_seed_credit(
                session,
                args.seed_credit_account,
                args.seed_credit,
                _money(args.expected_after) if args.expected_after is not None else None,
                args.apply,
            )

        findings = [
            _classify(session, entry, payment)
            for entry, payment in _lone_refund_debits(session)
        ]
        by_class: dict[str, list[Finding]] = {}
        for finding in findings:
            by_class.setdefault(finding.classification, []).append(finding)

        print(f"{'APPLY' if args.apply else 'DRY-RUN'}: {len(findings)} lone refund debits")
        for classification in sorted(by_class):
            rows = by_class[classification]
            total = _money(sum((row.entry.amount for row in rows), Decimal("0")))
            print(f"  {classification}: {len(rows)} rows, total {total}")

        _write_findings_csv(args.csv, findings)
        print(f"wrote {args.csv}")

        if (
            args.counterfactual
            or args.soft_delete_eligible
            or args.restore_construction_eligible
        ):
            reviews = _counterfactual_reviews(session, findings)
            _write_counterfactual_csv(args.counterfactual_csv, reviews)
            print(f"wrote {args.counterfactual_csv}")
            counts: dict[str, int] = {}
            totals: dict[str, Decimal] = {}
            for review in reviews:
                counts[review.status] = counts.get(review.status, 0) + 1
                totals[review.status] = totals.get(review.status, Decimal("0")) + review.suspect_total
            for status in sorted(counts):
                print(f"  {status}: {counts[status]} accounts, total {_money(totals[status])}")
            if args.soft_delete_eligible:
                return _soft_delete_eligible(session, reviews, args.apply)
            if args.restore_construction_eligible:
                return _restore_construction_eligible(session, reviews, args.apply)

        if args.apply:
            print(
                "No apply action requested. Use --soft-delete-eligible --apply "
                "or --restore-construction-eligible --apply."
            )
        return 0
    finally:
        session.close()


if __name__ == "__main__":
    sys.exit(main())
