"""Export or reverse phantom prepaid opening-balance debit rows.

The cutover opening-balance seed imported some non-negative deposit accounts as
active debit ledger rows. A non-negative imported deposit means the customer did
not owe opening debt, so the debit corrupts account net-ledger displays. This
tool is retired for normal operation. It is dry-run by default and creates
compensating credit entries only for rows that are safely classified as
phantom.

The original debit rows are left active for audit; the compensating credit nets
the statement to zero and is idempotent by original ledger-entry id.

Rows that already have an inactive phantom-reversal credit are never eligible.
That case means a later cleanup soft-deleted the debit/reversal audit pair; a
subsequent repair may have deliberately restored the original debit. Treating
the inactive credit as reusable evidence would re-reverse legitimate cutover
construction rows.

Examples
--------
  python -m scripts.one_off.reconcile_phantom_opening_debits
  python -m scripts.one_off.reconcile_phantom_opening_debits --scope non-terminal
  python -m scripts.one_off.reconcile_phantom_opening_debits --scope active --apply
"""

from __future__ import annotations

import argparse
import csv
import re
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db import SessionLocal
from app.models.billing import (
    LedgerCategory,
    LedgerEntry,
    LedgerEntryType,
    LedgerSource,
)
from app.models.subscriber import Subscriber, SubscriberStatus
from app.services.common import round_money, to_decimal

OPENING_MEMO = "Prepaid opening balance @ cutover"
LEGACY_REVERSAL_MEMO = "Reversal of prepaid opening balance cutover debit [id={id}]"
REVERSAL_MEMO = "Reversal of phantom prepaid opening balance cutover debit [id={id}]"
REVERSAL_CORRECTION_MEMO = (
    "Correction: remove excess opening-balance reversal credit [id={id}]"
)
DEFAULT_OUTPUT = "scratchpad/phantom_opening_debits.csv"

SCOPE_STATUSES: dict[str, set[SubscriberStatus]] = {
    "active": {SubscriberStatus.active},
    "non-terminal": {
        SubscriberStatus.active,
        SubscriberStatus.blocked,
        SubscriberStatus.suspended,
        SubscriberStatus.delinquent,
        SubscriberStatus.new,
    },
    "all": set(SubscriberStatus),
}

_LEGACY_REVERSAL_RE = re.compile(
    r"^Reversal of prepaid opening balance cutover debit \[id=([0-9a-f-]+)\]$"
)
_REVERSAL_RE = re.compile(
    r"^Reversal of phantom prepaid opening balance cutover debit \[id=([0-9a-f-]+)\]$"
)
_CORRECTION_RE = re.compile(
    r"^Correction: remove excess opening-balance reversal credit "
    r"\[id=([0-9a-f-]+)\]$"
)


@dataclass(frozen=True)
class Candidate:
    entry: LedgerEntry
    subscriber: Subscriber
    account_id: str
    existing_reversal: Decimal
    inactive_reversal: Decimal
    remaining: Decimal
    classification: str
    eligible: bool


def _deposit_value(subscriber: Subscriber) -> Decimal | None:
    if subscriber.deposit is None:
        return None
    return round_money(to_decimal(subscriber.deposit))


def _reversal_original_id(memo: str | None) -> str | None:
    if not memo:
        return None
    for pattern in (_LEGACY_REVERSAL_RE, _REVERSAL_RE):
        match = pattern.match(memo)
        if match:
            return match.group(1)
    return None


def _correction_reversal_id(memo: str | None) -> str | None:
    if not memo:
        return None
    match = _CORRECTION_RE.match(memo)
    return match.group(1) if match else None


def _reversal_amounts(
    db: Session,
    entry_ids: list[UUID],
) -> tuple[dict[str, Decimal], dict[str, Decimal]]:
    if not entry_ids:
        return {}, {}
    reversal_memos = [
        memo
        for entry_id in entry_ids
        for memo in (
            LEGACY_REVERSAL_MEMO.format(id=entry_id),
            REVERSAL_MEMO.format(id=entry_id),
        )
    ]
    reversals = list(
        db.scalars(
            select(LedgerEntry)
            .where(LedgerEntry.memo.in_(reversal_memos))
            .where(LedgerEntry.entry_type == LedgerEntryType.credit)
        ).all()
    )
    correction_by_reversal_id: dict[str, Decimal] = {}
    if reversals:
        correction_memos = [
            REVERSAL_CORRECTION_MEMO.format(id=reversal.id) for reversal in reversals
        ]
        corrections = list(
            db.scalars(
                select(LedgerEntry)
                .where(LedgerEntry.memo.in_(correction_memos))
                .where(LedgerEntry.entry_type == LedgerEntryType.debit)
                .where(LedgerEntry.is_active.is_(True))
            ).all()
        )
        for correction in corrections:
            reversal_id = _correction_reversal_id(correction.memo)
            if reversal_id:
                correction_by_reversal_id[reversal_id] = round_money(
                    correction_by_reversal_id.get(reversal_id, Decimal("0.00"))
                    + to_decimal(correction.amount)
                )

    active_by_original_id: dict[str, Decimal] = {}
    inactive_by_original_id: dict[str, Decimal] = {}
    for reversal in reversals:
        original_id = _reversal_original_id(reversal.memo)
        if not original_id:
            continue
        net = round_money(
            to_decimal(reversal.amount)
            - correction_by_reversal_id.get(str(reversal.id), Decimal("0.00"))
        )
        target = (
            active_by_original_id if reversal.is_active else inactive_by_original_id
        )
        target[original_id] = round_money(
            target.get(original_id, Decimal("0.00")) + net
        )
    return active_by_original_id, inactive_by_original_id


def _classify(
    subscriber: Subscriber,
    deposit: Decimal | None,
    remaining: Decimal,
    inactive_reversal: Decimal,
    scope_statuses: set[SubscriberStatus],
) -> tuple[str, bool]:
    if remaining <= 0:
        return "already_compensated", False
    if inactive_reversal > 0:
        return "previously_reversed_inactive_review", False
    if deposit is None:
        return "deposit_null_review", False
    if deposit < 0:
        return "legit_negative_deposit", False
    if subscriber.status not in scope_statuses:
        if subscriber.status in {SubscriberStatus.disabled, SubscriberStatus.canceled}:
            return "terminal_nonnegative_review", False
        return "scope_excluded_nonnegative_deposit", False
    return "eligible_phantom_debit", True


def _load_candidates(db: Session, scope: str) -> list[Candidate]:
    scope_statuses = SCOPE_STATUSES[scope]
    rows = (
        db.query(LedgerEntry, Subscriber)
        .join(Subscriber, Subscriber.id == LedgerEntry.account_id)
        .filter(LedgerEntry.memo == OPENING_MEMO)
        .filter(LedgerEntry.entry_type == LedgerEntryType.debit)
        .filter(LedgerEntry.is_active.is_(True))
        .order_by(Subscriber.status.asc(), Subscriber.id.asc(), LedgerEntry.id.asc())
        .all()
    )
    reversal_by_entry_id, inactive_reversal_by_entry_id = _reversal_amounts(
        db,
        [entry.id for entry, _subscriber in rows],
    )
    candidates: list[Candidate] = []
    for entry, subscriber in rows:
        existing = reversal_by_entry_id.get(str(entry.id), Decimal("0.00"))
        inactive = inactive_reversal_by_entry_id.get(str(entry.id), Decimal("0.00"))
        amount = round_money(to_decimal(entry.amount))
        remaining = round_money(max(amount - existing, Decimal("0.00")))
        deposit = _deposit_value(subscriber)
        classification, eligible = _classify(
            subscriber,
            deposit,
            remaining,
            inactive,
            scope_statuses,
        )
        candidates.append(
            Candidate(
                entry=entry,
                subscriber=subscriber,
                account_id=str(entry.account_id),
                existing_reversal=existing,
                inactive_reversal=inactive,
                remaining=remaining,
                classification=classification,
                eligible=eligible,
            )
        )
    return candidates


def _subscriber_label(subscriber: Subscriber) -> str:
    return (
        subscriber.display_name
        or subscriber.company_name
        or f"{subscriber.first_name} {subscriber.last_name}".strip()
    )


def _candidate_row(candidate: Candidate) -> dict[str, str]:
    subscriber = candidate.subscriber
    entry = candidate.entry
    deposit = _deposit_value(subscriber)
    return {
        "classification": candidate.classification,
        "eligible": "yes" if candidate.eligible else "no",
        "ledger_entry_id": str(entry.id),
        "account_id": str(entry.account_id),
        "subscriber_status": subscriber.status.value,
        "subscriber_number": subscriber.subscriber_number or "",
        "subscriber_name": _subscriber_label(subscriber),
        "deposit": "" if deposit is None else str(deposit),
        "debit_amount": str(round_money(to_decimal(entry.amount))),
        "existing_reversal": str(candidate.existing_reversal),
        "inactive_reversal": str(candidate.inactive_reversal),
        "remaining_to_reverse": str(candidate.remaining),
        "currency": entry.currency or "NGN",
        "source": entry.source.value if entry.source else "",
        "category": entry.category.value if entry.category else "",
        "memo": entry.memo or "",
        "created_at": entry.created_at.isoformat() if entry.created_at else "",
    }


def _write_csv(path: Path, candidates: list[Candidate]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    rows = [_candidate_row(candidate) for candidate in candidates]
    fieldnames = (
        list(rows[0].keys())
        if rows
        else [
            "classification",
            "eligible",
            "ledger_entry_id",
            "account_id",
            "subscriber_status",
            "subscriber_number",
            "subscriber_name",
            "deposit",
            "debit_amount",
            "existing_reversal",
            "inactive_reversal",
            "remaining_to_reverse",
            "currency",
            "source",
            "category",
            "memo",
            "created_at",
        ]
    )
    with path.open("w", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)


def _apply(db: Session, candidates: list[Candidate]) -> int:
    written = 0
    for candidate in candidates:
        if not candidate.eligible or candidate.remaining <= 0:
            continue
        entry = candidate.entry
        db.add(
            LedgerEntry(
                account_id=entry.account_id,
                invoice_id=None,
                payment_id=None,
                entry_type=LedgerEntryType.credit,
                source=LedgerSource.adjustment,
                category=LedgerCategory.deposit,
                amount=candidate.remaining,
                currency=entry.currency or "NGN",
                memo=REVERSAL_MEMO.format(id=entry.id),
            )
        )
        written += 1
    if written:
        db.commit()
    return written


def _print_summary(candidates: list[Candidate], *, scope: str, applied: int) -> None:
    print(f"Phantom opening debit reconcile scope={scope}")
    print(f"rows scanned: {len(candidates)}")
    if applied:
        print(f"reversal credits written: {applied}")
    counts: dict[str, int] = {}
    totals: dict[str, Decimal] = {}
    accounts: dict[str, set[str]] = {}
    for candidate in candidates:
        key = candidate.classification
        counts[key] = counts.get(key, 0) + 1
        totals[key] = totals.get(key, Decimal("0.00")) + candidate.remaining
        accounts.setdefault(key, set()).add(candidate.account_id)
    for key in sorted(counts):
        print(
            f"  {key}: rows={counts[key]} accounts={len(accounts[key])} "
            f"remaining={round_money(totals[key])}"
        )
    eligible_total = sum(
        (candidate.remaining for candidate in candidates if candidate.eligible),
        Decimal("0.00"),
    )
    print(f"eligible_total: {round_money(eligible_total)}")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--scope",
        choices=sorted(SCOPE_STATUSES),
        default="active",
        help=(
            "Which non-negative-deposit rows are eligible for apply. "
            "All rows are still exported and classified."
        ),
    )
    parser.add_argument("--output", default=DEFAULT_OUTPUT)
    parser.add_argument(
        "--apply",
        action="store_true",
        help="Write compensating credits. Without this flag the script is read-only.",
    )
    args = parser.parse_args()

    db = SessionLocal()
    applied = 0
    try:
        candidates = _load_candidates(db, args.scope)
        _write_csv(Path(args.output), candidates)
        if args.apply:
            applied = _apply(db, candidates)
    finally:
        db.close()

    mode = "APPLY" if args.apply else "DRY-RUN"
    print(f"=== {mode} ===")
    _print_summary(candidates, scope=args.scope, applied=applied)
    print(f"output: {args.output}")
    if not args.apply:
        print("Re-run with --apply to write eligible compensating credits.")


if __name__ == "__main__":
    main()
