"""Counterfactual audit for June 24 phantom opening-balance reversals.

The June 24 cleanup treated some cutover opening-balance construction debits as
phantoms and reversed them with credits. The July 2 display cleanup then
soft-deleted both rows in each debit/reversal pair. For rows where the original
debit was legitimate construction, that left the account over-credited.

This report groups every phantom opening reversal by account and checks the
same source-of-truth identity used by the void-mirror audit:

    target = deposit + post-cutover succeeded payments - post-cutover invoices

If the account's current available balance is above target by exactly the sum
of inactive original opening debits that have matching inactive June 24 reversal
credits, the account is eligible for ``--restore-eligible --apply``. The apply
path reactivates the original construction debit rows only; it does not mint new
rows and leaves the inactive reversal credits as audit evidence.

Dry-run by default.
"""

from __future__ import annotations

import argparse
import csv
import re
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
    LedgerEntry,
    LedgerEntryType,
    LedgerSource,
    Payment,
    PaymentStatus,
)
from app.models.collections import DunningActionLog, DunningCase
from app.models.subscriber import Subscriber
from app.services.common import round_money

OPENING_MEMO = "Prepaid opening balance @ cutover"
PHANTOM_REVERSAL_PREFIX = (
    "Reversal of phantom prepaid opening balance cutover debit [id="
)
TOLERANCE = Decimal("0.01")
CUTOVER_AT = datetime(2026, 6, 16, tzinfo=UTC)
DEFAULT_OUTPUT = "scratchpad/phantom_opening_reversal_counterfactual.csv"
ID_RE = re.compile(r"\[id=([0-9a-f-]{36})\]")


@dataclass(frozen=True)
class Pair:
    original: LedgerEntry | None
    reversal: LedgerEntry
    valid: bool
    reason: str


@dataclass(frozen=True)
class Review:
    account_id: uuid.UUID
    subscriber_name: str
    subscriber_status: str
    status: str
    pair_count: int
    invalid_pairs: int
    inactive_original_amount: Decimal
    active_original_amount: Decimal
    active_reversal_amount: Decimal
    inactive_reversal_amount: Decimal
    restore_needed: Decimal
    deposit: Decimal
    post_cutover_payments: Decimal
    post_cutover_invoices: Decimal
    target_available: Decimal
    current_available: Decimal
    gap: Decimal
    gap_after_restore: Decimal
    post_adjustment_rows: int
    post_adjustment_net: Decimal
    dunning_events: int
    status_events: int
    event_store_events: int
    direction: str
    residual_after_pair_restore: Decimal
    cause_bucket: str
    original_ids: str
    reversal_ids: str


def _money(value: Decimal | int | float | str | None) -> Decimal:
    return round_money(value or Decimal("0"))


def _eq(a: Decimal | None, b: Decimal | None) -> bool:
    if a is None or b is None:
        return False
    return abs(a - b) <= TOLERANCE


def _referenced_id(memo: str | None) -> uuid.UUID | None:
    match = ID_RE.search(memo or "")
    return uuid.UUID(match.group(1)) if match else None


def _subscriber_name(subscriber: Subscriber | None) -> str:
    if subscriber is None:
        return ""
    for value in (subscriber.display_name, subscriber.company_name):
        if value:
            return value
    return f"{subscriber.first_name or ''} {subscriber.last_name or ''}".strip()


def _collect_pairs(session: Session) -> list[Pair]:
    reversals = (
        session.execute(
            select(LedgerEntry).where(
                LedgerEntry.memo.like(f"{PHANTOM_REVERSAL_PREFIX}%"),
                LedgerEntry.entry_type == LedgerEntryType.credit,
            )
        )
        .scalars()
        .all()
    )
    pairs: list[Pair] = []
    for reversal in reversals:
        original_id = _referenced_id(reversal.memo)
        original = session.get(LedgerEntry, original_id) if original_id else None
        reason = ""
        if original is None:
            reason = "missing_original"
        elif original.account_id != reversal.account_id:
            reason = "account_mismatch"
        elif not _eq(_money(original.amount), _money(reversal.amount)):
            reason = "amount_mismatch"
        elif original.entry_type is not LedgerEntryType.debit:
            reason = "original_not_debit"
        elif original.source is not LedgerSource.adjustment:
            reason = "original_not_adjustment"
        elif original.memo != OPENING_MEMO:
            reason = "original_memo_mismatch"
        elif original.invoice_id is not None or original.payment_id is not None:
            reason = "original_linked"
        pairs.append(Pair(original, reversal, reason == "", reason))
    return pairs


def _post_cutover_succeeded_payment_map(
    session: Session, account_ids: list[uuid.UUID]
) -> dict[uuid.UUID, Decimal]:
    if not account_ids:
        return {}
    rows = session.execute(
        select(Payment.account_id, func.coalesce(func.sum(Payment.amount), 0))
        .where(
            Payment.account_id.in_(account_ids),
            Payment.is_active.is_(True),
            Payment.status == PaymentStatus.succeeded,
            Payment.created_at >= CUTOVER_AT,
        )
        .group_by(Payment.account_id)
    ).all()
    return {account_id: _money(total) for account_id, total in rows}


def _post_cutover_invoice_total_map(
    session: Session, account_ids: list[uuid.UUID]
) -> dict[uuid.UUID, Decimal]:
    if not account_ids:
        return {}
    invoice_rows = session.execute(
        select(Invoice.account_id, func.coalesce(func.sum(Invoice.total), 0))
        .where(
            Invoice.account_id.in_(account_ids),
            Invoice.is_active.is_(True),
            Invoice.status != InvoiceStatus.void,
            Invoice.is_proforma.isnot(True),
            Invoice.created_at >= CUTOVER_AT,
        )
        .group_by(Invoice.account_id)
    ).all()
    totals = {account_id: _money(total) for account_id, total in invoice_rows}
    ledger_charge_rows = session.execute(
        select(
            LedgerEntry.account_id,
            func.coalesce(func.sum(LedgerEntry.amount), 0),
        )
        .where(
            LedgerEntry.account_id.in_(account_ids),
            LedgerEntry.is_active.is_(True),
            LedgerEntry.invoice_id.is_(None),
            LedgerEntry.entry_type == LedgerEntryType.debit,
            LedgerEntry.source == LedgerSource.invoice,
            LedgerEntry.currency == "NGN",
            LedgerEntry.created_at >= CUTOVER_AT,
        )
        .group_by(LedgerEntry.account_id)
    ).all()
    for account_id, total in ledger_charge_rows:
        totals[account_id] = _money(
            totals.get(account_id, Decimal("0")) + _money(total)
        )
    return totals


def _current_available_map(
    session: Session, account_ids: list[uuid.UUID]
) -> dict[uuid.UUID, Decimal]:
    if not account_ids:
        return {}
    ledger_signed = case(
        (LedgerEntry.entry_type == LedgerEntryType.credit, LedgerEntry.amount),
        else_=-LedgerEntry.amount,
    )
    ledger_rows = session.execute(
        select(
            LedgerEntry.account_id,
            func.coalesce(func.sum(ledger_signed), 0),
        )
        .where(
            LedgerEntry.account_id.in_(account_ids),
            LedgerEntry.is_active.is_(True),
            LedgerEntry.invoice_id.is_(None),
            LedgerEntry.currency == "NGN",
        )
        .group_by(LedgerEntry.account_id)
    ).all()
    open_rows = session.execute(
        select(Invoice.account_id, func.coalesce(func.sum(Invoice.balance_due), 0))
        .where(
            Invoice.account_id.in_(account_ids),
            Invoice.is_active.is_(True),
            Invoice.balance_due > 0,
            Invoice.status.in_(
                [
                    InvoiceStatus.issued,
                    InvoiceStatus.partially_paid,
                    InvoiceStatus.overdue,
                ]
            ),
            Invoice.currency == "NGN",
        )
        .group_by(Invoice.account_id)
    ).all()
    ledger_by_account = {account_id: _money(total) for account_id, total in ledger_rows}
    open_by_account = {account_id: _money(total) for account_id, total in open_rows}
    return {
        account_id: _money(
            ledger_by_account.get(account_id, Decimal("0"))
            - open_by_account.get(account_id, Decimal("0"))
        )
        for account_id in account_ids
    }


def _post_adjustment_warning_map(
    session: Session, account_ids: list[uuid.UUID]
) -> dict[uuid.UUID, tuple[int, Decimal]]:
    if not account_ids:
        return {}
    signed = case(
        (LedgerEntry.entry_type == LedgerEntryType.credit, LedgerEntry.amount),
        else_=-LedgerEntry.amount,
    )
    rows = session.execute(
        select(
            LedgerEntry.account_id,
            func.count(LedgerEntry.id),
            func.coalesce(func.sum(signed), 0),
        )
        .where(
            LedgerEntry.account_id.in_(account_ids),
            LedgerEntry.is_active.is_(True),
            LedgerEntry.invoice_id.is_(None),
            LedgerEntry.source == LedgerSource.adjustment,
            LedgerEntry.memo != OPENING_MEMO,
            LedgerEntry.created_at >= CUTOVER_AT,
        )
        .group_by(LedgerEntry.account_id)
    ).all()
    return {
        account_id: (int(count or 0), _money(total))
        for account_id, count, total in rows
    }


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


def _event_store_count(session: Session, account_id: uuid.UUID, since: datetime) -> int:
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


def _direction(restore_needed: Decimal) -> str:
    if _eq(restore_needed, Decimal("0")):
        return "balanced"
    if restore_needed > 0:
        return "overcredited"
    return "understated"


def _cause_bucket(
    *,
    status: str,
    invalid_pairs: int,
    active_reversal_amount: Decimal,
    inactive_original_amount: Decimal,
    restore_needed: Decimal,
    residual_after_pair_restore: Decimal,
    post_adjustment_net: Decimal,
) -> str:
    if invalid_pairs:
        return "invalid_pair"
    if active_reversal_amount > 0:
        return "active_reversal_present"
    if status == "already_restored":
        return "already_restored"
    if status == "eligible_restore_construction":
        return "exact_inactive_pair_restore"
    if status == "review_exact_with_post_adjustments":
        return "exact_pair_but_post_cutover_adjustments_present"
    if status == "inactive_pair_balanced_review":
        if not _eq(post_adjustment_net, Decimal("0")):
            return "balanced_due_to_post_cutover_adjustment"
        return "balanced_with_inactive_pair"
    if restore_needed < 0:
        return "local_books_understate_mirror_truth"
    if inactive_original_amount > 0:
        residual = _money(restore_needed - inactive_original_amount)
        if not _eq(post_adjustment_net, Decimal("0")) and _eq(
            residual, post_adjustment_net
        ):
            return "post_cutover_adjustment_explains_residual"
        if residual > 0:
            return "restore_plus_extra_overcredit_gap"
        if residual < 0:
            return "partial_restore_only_full_pair_would_overcorrect"
        if not _eq(residual_after_pair_restore, Decimal("0")):
            return "nonzero_residual_after_pair_restore"
    return "manual_unclassified"


def _build_reviews(
    session: Session, pairs: list[Pair], include_events: bool
) -> list[Review]:
    grouped: dict[uuid.UUID, list[Pair]] = {}
    for pair in pairs:
        grouped.setdefault(pair.reversal.account_id, []).append(pair)
    account_ids = list(grouped)
    current_by_account = _current_available_map(session, account_ids)
    post_payments_by_account = _post_cutover_succeeded_payment_map(session, account_ids)
    post_invoices_by_account = _post_cutover_invoice_total_map(session, account_ids)
    post_adjustment_by_account = _post_adjustment_warning_map(session, account_ids)

    reviews: list[Review] = []
    for account_id, rows in grouped.items():
        subscriber = session.get(Subscriber, account_id)
        deposit = _money(subscriber.deposit if subscriber else None)
        post_payments = post_payments_by_account.get(account_id, Decimal("0"))
        post_invoices = post_invoices_by_account.get(account_id, Decimal("0"))
        target = _money(deposit + post_payments - post_invoices)
        current = current_by_account.get(account_id, Decimal("0"))
        gap = _money(target - current)
        restore_needed = _money(current - target)
        invalid_pairs = sum(1 for row in rows if not row.valid)
        inactive_original_amount = _money(
            sum(
                (
                    row.original.amount
                    for row in rows
                    if row.valid
                    and row.original is not None
                    and not row.original.is_active
                    and not row.reversal.is_active
                ),
                Decimal("0"),
            )
        )
        active_original_amount = _money(
            sum(
                (
                    row.original.amount
                    for row in rows
                    if row.valid and row.original is not None and row.original.is_active
                ),
                Decimal("0"),
            )
        )
        active_reversal_amount = _money(
            sum(
                (
                    row.reversal.amount
                    for row in rows
                    if row.valid and row.reversal.is_active
                ),
                Decimal("0"),
            )
        )
        inactive_reversal_amount = _money(
            sum(
                (
                    row.reversal.amount
                    for row in rows
                    if row.valid and not row.reversal.is_active
                ),
                Decimal("0"),
            )
        )
        gap_after_restore = _money(gap + inactive_original_amount)
        residual_after_pair_restore = gap_after_restore
        adjustment_rows, adjustment_net = post_adjustment_by_account.get(
            account_id, (0, Decimal("0"))
        )
        first_reversal_at = min(row.reversal.created_at for row in rows)
        dunning_events = status_events = event_store_events = 0
        if include_events:
            dunning_events = _dunning_event_count(
                session, account_id, first_reversal_at
            )
            status_events = _status_event_count(session, account_id, first_reversal_at)
            event_store_events = _event_store_count(
                session, account_id, first_reversal_at
            )

        if invalid_pairs:
            status = "invalid_pair_review"
        elif active_reversal_amount > 0:
            status = "active_reversal_review"
        elif inactive_original_amount > 0 and _eq(
            restore_needed, inactive_original_amount
        ):
            status = (
                "eligible_restore_construction"
                if _eq(adjustment_net, Decimal("0"))
                else "review_exact_with_post_adjustments"
            )
        elif active_original_amount > 0 and _eq(current, target):
            status = "already_restored"
        elif inactive_original_amount > 0 and _eq(current, target):
            status = "inactive_pair_balanced_review"
        elif _eq(current, target):
            status = "balanced"
        elif restore_needed < 0:
            status = "manual_understated_review"
        else:
            status = "manual_review"
        cause_bucket = _cause_bucket(
            status=status,
            invalid_pairs=invalid_pairs,
            active_reversal_amount=active_reversal_amount,
            inactive_original_amount=inactive_original_amount,
            restore_needed=restore_needed,
            residual_after_pair_restore=residual_after_pair_restore,
            post_adjustment_net=adjustment_net,
        )

        reviews.append(
            Review(
                account_id=account_id,
                subscriber_name=_subscriber_name(subscriber),
                subscriber_status=getattr(
                    getattr(subscriber, "status", None), "value", ""
                ),
                status=status,
                pair_count=len(rows),
                invalid_pairs=invalid_pairs,
                inactive_original_amount=inactive_original_amount,
                active_original_amount=active_original_amount,
                active_reversal_amount=active_reversal_amount,
                inactive_reversal_amount=inactive_reversal_amount,
                restore_needed=restore_needed,
                deposit=deposit,
                post_cutover_payments=post_payments,
                post_cutover_invoices=post_invoices,
                target_available=target,
                current_available=current,
                gap=gap,
                gap_after_restore=gap_after_restore,
                post_adjustment_rows=adjustment_rows,
                post_adjustment_net=adjustment_net,
                dunning_events=dunning_events,
                status_events=status_events,
                event_store_events=event_store_events,
                direction=_direction(restore_needed),
                residual_after_pair_restore=residual_after_pair_restore,
                cause_bucket=cause_bucket,
                original_ids=", ".join(
                    str(row.original.id) for row in rows if row.original is not None
                ),
                reversal_ids=", ".join(str(row.reversal.id) for row in rows),
            )
        )

    return sorted(
        reviews,
        key=lambda row: (
            row.status != "eligible_restore_construction",
            -row.inactive_original_amount,
            row.subscriber_name,
        ),
    )


def _write_csv(path: Path, reviews: list[Review]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="") as handle:
        writer = csv.writer(handle)
        writer.writerow(
            [
                "status",
                "account_id",
                "subscriber_name",
                "subscriber_status",
                "pair_count",
                "invalid_pairs",
                "inactive_original_amount",
                "active_original_amount",
                "active_reversal_amount",
                "inactive_reversal_amount",
                "restore_needed",
                "deposit",
                "post_cutover_succeeded_payments",
                "post_cutover_invoice_totals",
                "target_available",
                "current_available",
                "gap",
                "gap_after_restore",
                "post_adjustment_rows",
                "post_adjustment_net",
                "dunning_events_after_first_reversal",
                "status_events_after_first_reversal",
                "event_store_events_after_first_reversal",
                "direction",
                "residual_after_pair_restore",
                "cause_bucket",
                "original_entry_ids",
                "reversal_entry_ids",
            ]
        )
        for review in reviews:
            writer.writerow(
                [
                    review.status,
                    review.account_id,
                    review.subscriber_name,
                    review.subscriber_status,
                    review.pair_count,
                    review.invalid_pairs,
                    review.inactive_original_amount,
                    review.active_original_amount,
                    review.active_reversal_amount,
                    review.inactive_reversal_amount,
                    review.restore_needed,
                    review.deposit,
                    review.post_cutover_payments,
                    review.post_cutover_invoices,
                    review.target_available,
                    review.current_available,
                    review.gap,
                    review.gap_after_restore,
                    review.post_adjustment_rows,
                    review.post_adjustment_net,
                    review.dunning_events,
                    review.status_events,
                    review.event_store_events,
                    review.direction,
                    review.residual_after_pair_restore,
                    review.cause_bucket,
                    review.original_ids,
                    review.reversal_ids,
                ]
            )


def _restore_eligible(
    session: Session,
    pairs: list[Pair],
    reviews: list[Review],
    apply: bool,
    print_limit: int,
) -> int:
    eligible_account_ids = {
        review.account_id
        for review in reviews
        if review.status == "eligible_restore_construction"
    }
    entries = [
        pair.original
        for pair in pairs
        if pair.valid
        and pair.original is not None
        and pair.original.account_id in eligible_account_ids
        and not pair.original.is_active
        and not pair.reversal.is_active
    ]
    total = _money(sum((entry.amount for entry in entries), Decimal("0")))
    print(
        f"{'APPLY' if apply else 'DRY-RUN'}: restore {len(entries)} "
        f"opening construction debit rows totaling {total}"
    )
    printed = 0
    suppressed = 0
    for review in reviews:
        if review.account_id not in eligible_account_ids:
            continue
        expected_after = _money(
            review.current_available - review.inactive_original_amount
        )
        if printed < print_limit:
            print(
                f"  {review.subscriber_name}: {review.current_available} -> "
                f"{expected_after} (target {review.target_available})"
            )
            printed += 1
        else:
            suppressed += 1
    if suppressed:
        print(f"  ... {suppressed} additional eligible accounts omitted from output")
    if not apply:
        return 0

    for entry in entries:
        entry.is_active = True
    session.flush()

    failures = []
    actual_by_account = _current_available_map(session, list(eligible_account_ids))
    for review in reviews:
        if review.account_id not in eligible_account_ids:
            continue
        actual = actual_by_account.get(review.account_id, Decimal("0"))
        if not _eq(actual, review.target_available):
            failures.append((review.subscriber_name, actual, review.target_available))
    if failures:
        session.rollback()
        for name, actual, target in failures:
            print(f"VERIFY FAILED: {name}: available {actual}, expected {target}")
        return 1
    session.commit()
    print(f"restored {len(entries)} opening construction debit rows")
    return 0


def _print_summary(reviews: list[Review]) -> None:
    counts: dict[str, int] = {}
    restore_totals: dict[str, Decimal] = {}
    cause_counts: dict[str, int] = {}
    cause_residuals: dict[str, Decimal] = {}
    for review in reviews:
        counts[review.status] = counts.get(review.status, 0) + 1
        restore_totals[review.status] = (
            restore_totals.get(review.status, Decimal("0"))
            + review.inactive_original_amount
        )
        if review.status != "already_restored":
            cause_counts[review.cause_bucket] = (
                cause_counts.get(review.cause_bucket, 0) + 1
            )
            cause_residuals[review.cause_bucket] = cause_residuals.get(
                review.cause_bucket, Decimal("0")
            ) + abs(review.residual_after_pair_restore)
    for status in sorted(counts):
        print(
            f"  {status}: {counts[status]} accounts, "
            f"inactive_original_total {_money(restore_totals[status])}"
        )
    if cause_counts:
        print("review cause buckets:")
        for bucket in sorted(cause_counts):
            print(
                f"  {bucket}: {cause_counts[bucket]} accounts, "
                f"abs_residual_total {_money(cause_residuals[bucket])}"
            )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply", action="store_true", help="write changes")
    parser.add_argument("--csv", type=Path, default=Path(DEFAULT_OUTPUT))
    parser.add_argument(
        "--restore-eligible",
        action="store_true",
        help="reactivate exact-match inactive construction debits",
    )
    parser.add_argument(
        "--include-events",
        action="store_true",
        help="include per-account dunning/status/event counts; slower",
    )
    parser.add_argument(
        "--print-limit",
        type=int,
        default=25,
        help="maximum eligible account transitions to print",
    )
    args = parser.parse_args()

    session = SessionLocal()
    try:
        pairs = _collect_pairs(session)
        reviews = _build_reviews(session, pairs, args.include_events)
        print(
            f"{'APPLY' if args.apply else 'DRY-RUN'}: {len(pairs)} "
            f"phantom opening reversal pairs across {len(reviews)} accounts"
        )
        _write_csv(args.csv, reviews)
        print(f"wrote {args.csv}")
        _print_summary(reviews)
        if args.restore_eligible:
            return _restore_eligible(
                session, pairs, reviews, args.apply, max(args.print_limit, 0)
            )
        if args.apply:
            print("No apply action requested. Use --restore-eligible --apply.")
        return 0
    finally:
        session.close()


if __name__ == "__main__":
    sys.exit(main())
