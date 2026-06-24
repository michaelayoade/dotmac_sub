#!/usr/bin/env python3
"""Reverse erroneous prepaid drawdown ledger debits for monthly plans.

During cutover a prepaid drawdown path posted direct ledger DEBIT entries with
memo ``Prepaid charge: 30d (...)``. Those rows belong only to true drawdown
plans, but all current prepaid offers are monthly-cycle plans billed by invoice
with VAT. Leaving the rows in place makes monthly customers appear under-funded
and can double-bill them once monthly invoicing also runs.

This script is append-only: it never deletes or edits the bad rows. It writes one
compensating CREDIT per eligible DEBIT, with the original ledger entry id in the
memo so the operation is auditable and idempotent.

Dry-run by default:

    python -m scripts.one_off.reverse_erroneous_prepaid_drawdown
    python -m scripts.one_off.reverse_erroneous_prepaid_drawdown --apply
"""

from __future__ import annotations

import argparse
import csv
import re
import sys
import uuid
from collections import defaultdict
from dataclasses import dataclass
from datetime import UTC, date, datetime, time, timedelta
from decimal import Decimal
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent.parent))

from app.db import SessionLocal  # noqa: E402
from app.models.billing import (  # noqa: E402
    LedgerEntry,
    LedgerEntryType,
    LedgerSource,
)
from app.models.catalog import BillingCycle, CatalogOffer, Subscription  # noqa: E402
from app.models.subscriber import Subscriber  # noqa: E402

PREPAID_DRAWDOWN_MEMO_PREFIX = "Prepaid charge: 30d"
REVERSAL_MEMO_PREFIX = "Reversal of erroneous prepaid drawdown charge"
SUBSCRIPTION_ID_RE = re.compile(r"sub=([0-9a-fA-F-]{36})")


@dataclass(frozen=True)
class Row:
    classification: str
    ledger_entry_id: str
    reversal_entry_id: str
    created_at: str
    effective_date: str
    account_id: str
    subscriber_number: str
    display_name: str
    subscription_id: str
    offer_name: str
    billing_cycle: str
    amount: Decimal
    currency: str
    original_memo: str
    reversal_memo: str


def _parse_date(value: str) -> date:
    try:
        return date.fromisoformat(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError(
            f"{value!r} must be an ISO date like 2026-06-16"
        ) from exc


def _day_start(value: date) -> datetime:
    return datetime.combine(value, time.min, tzinfo=UTC)


def _fmt_dt(value: datetime | None) -> str:
    return value.isoformat() if value else ""


def _enum_value(value: object) -> str:
    return str(getattr(value, "value", value) or "")


def _money(value: Decimal | None) -> Decimal:
    return (value or Decimal("0.00")).quantize(Decimal("0.01"))


def _subscription_id_from_memo(memo: str | None) -> uuid.UUID | None:
    if not memo:
        return None
    match = SUBSCRIPTION_ID_RE.search(memo)
    if not match:
        return None
    return uuid.UUID(match.group(1))


def _reversal_memo(entry_id: uuid.UUID) -> str:
    return f"{REVERSAL_MEMO_PREFIX} [original_ledger_entry_id={entry_id}]"


def _existing_reversal_id(db, memo: str) -> uuid.UUID | None:
    return (
        db.query(LedgerEntry.id)
        .filter(LedgerEntry.entry_type == LedgerEntryType.credit)
        .filter(LedgerEntry.is_active.is_(True))
        .filter(LedgerEntry.memo == memo)
        .scalar()
    )


def _base_query(db, start_at: datetime, end_at: datetime, *, lock_rows: bool):
    query = (
        db.query(LedgerEntry)
        .filter(LedgerEntry.entry_type == LedgerEntryType.debit)
        .filter(LedgerEntry.is_active.is_(True))
        .filter(LedgerEntry.memo.ilike(f"{PREPAID_DRAWDOWN_MEMO_PREFIX}%"))
        .filter(LedgerEntry.created_at >= start_at)
        .filter(LedgerEntry.created_at < end_at)
        .order_by(LedgerEntry.created_at.asc(), LedgerEntry.id.asc())
    )
    if lock_rows:
        query = query.with_for_update(of=LedgerEntry)
    return query


def _classify_entry(db, entry: LedgerEntry) -> tuple[str, Subscription | None, str]:
    subscription_id = _subscription_id_from_memo(entry.memo)
    reversal_memo = _reversal_memo(entry.id)

    if _existing_reversal_id(db, reversal_memo):
        return "already_reversed", None, reversal_memo
    if subscription_id is None:
        return "skip_no_subscription_id", None, reversal_memo

    subscription = db.get(Subscription, subscription_id)
    if subscription is None:
        return "skip_missing_subscription", None, reversal_memo
    if subscription.subscriber_id != entry.account_id:
        return "skip_account_mismatch", subscription, reversal_memo

    offer = subscription.offer
    if offer is None:
        return "skip_missing_offer", subscription, reversal_memo
    if offer.billing_cycle != BillingCycle.monthly:
        return "skip_non_monthly_offer", subscription, reversal_memo

    return "eligible", subscription, reversal_memo


def _row(
    entry: LedgerEntry,
    classification: str,
    subscription: Subscription | None,
    reversal_id: uuid.UUID | None,
    reversal_memo: str,
) -> Row:
    subscriber: Subscriber | None = entry.account
    offer: CatalogOffer | None = subscription.offer if subscription else None
    display_name = ""
    if subscriber is not None:
        display_name = (
            subscriber.display_name
            or subscriber.company_name
            or f"{subscriber.first_name} {subscriber.last_name}".strip()
        )
    return Row(
        classification=classification,
        ledger_entry_id=str(entry.id),
        reversal_entry_id=str(reversal_id or ""),
        created_at=_fmt_dt(entry.created_at),
        effective_date=_fmt_dt(entry.effective_date),
        account_id=str(entry.account_id),
        subscriber_number=(subscriber.subscriber_number if subscriber else "") or "",
        display_name=display_name,
        subscription_id=str(subscription.id) if subscription else "",
        offer_name=(offer.name if offer else "") or "",
        billing_cycle=_enum_value(offer.billing_cycle if offer else ""),
        amount=_money(entry.amount),
        currency=entry.currency or "NGN",
        original_memo=entry.memo or "",
        reversal_memo=reversal_memo,
    )


def _write_csv(path: Path, rows: list[Row]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fieldnames = list(Row.__dataclass_fields__)
    with path.open("w", newline="") as fh:
        writer = csv.DictWriter(fh, fieldnames=fieldnames)
        writer.writeheader()
        for row in rows:
            writer.writerow(
                {
                    **row.__dict__,
                    "amount": f"{row.amount:.2f}",
                }
            )


def _print_summary(rows: list[Row], output: Path, *, dry_run: bool) -> None:
    counts: dict[str, int] = defaultdict(int)
    totals: dict[str, Decimal] = defaultdict(lambda: Decimal("0.00"))
    accounts: dict[str, set[str]] = defaultdict(set)
    for row in rows:
        counts[row.classification] += 1
        totals[row.classification] = _money(totals[row.classification] + row.amount)
        accounts[row.classification].add(row.account_id)

    mode = "DRY-RUN (no changes written)" if dry_run else "APPLY"
    print(f"\n=== Reverse erroneous prepaid drawdown — {mode} ===")
    for classification in sorted(counts):
        print(
            f"{classification:26} rows={counts[classification]:5} "
            f"accounts={len(accounts[classification]):5} "
            f"total={totals[classification]:,.2f}"
        )
    print(f"\nCSV: {output}")
    if dry_run:
        print("Re-run with --apply to write compensating credits.")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--from-date",
        type=_parse_date,
        default=date(2026, 6, 16),
        help="first created_at date to include (UTC, default: 2026-06-16)",
    )
    parser.add_argument(
        "--to-date",
        type=_parse_date,
        default=date(2026, 6, 22),
        help="last created_at date to include (UTC, inclusive; default: 2026-06-22)",
    )
    parser.add_argument(
        "--output",
        default="scratchpad/erroneous_prepaid_drawdown_reversal.csv",
        help="CSV artifact path",
    )
    parser.add_argument(
        "--apply",
        action="store_true",
        help="write compensating credits; default is read-only dry-run",
    )
    args = parser.parse_args()

    start_at = _day_start(args.from_date)
    end_at = _day_start(args.to_date + timedelta(days=1))
    output = Path(args.output)
    dry_run = not args.apply
    rows: list[Row] = []

    db = SessionLocal()
    try:
        entries = _base_query(db, start_at, end_at, lock_rows=args.apply).all()
        for entry in entries:
            classification, subscription, reversal_memo = _classify_entry(db, entry)
            reversal_id = _existing_reversal_id(db, reversal_memo)
            if args.apply and classification == "eligible":
                reversal = LedgerEntry(
                    account_id=entry.account_id,
                    invoice_id=None,
                    payment_id=None,
                    entry_type=LedgerEntryType.credit,
                    source=LedgerSource.adjustment,
                    category=entry.category,
                    amount=_money(entry.amount),
                    currency=entry.currency or "NGN",
                    memo=reversal_memo,
                    effective_date=entry.effective_date or entry.created_at,
                )
                db.add(reversal)
                db.flush()
                reversal_id = reversal.id
                classification = "reversed"

            rows.append(
                _row(
                    entry,
                    classification,
                    subscription,
                    reversal_id,
                    reversal_memo,
                )
            )

        if args.apply:
            db.commit()

        _write_csv(output, rows)
        _print_summary(rows, output, dry_run=dry_run)
    except Exception:
        if args.apply:
            db.rollback()
        raise
    finally:
        db.close()


if __name__ == "__main__":
    main()
