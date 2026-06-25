"""Reconcile erroneous prepaid drawdown debit reversals.

Bad rows have memo ``Prepaid charge: 30d (...)`` and should be fully offset by a
credit memo ``Reversal of erroneous prepaid drawdown charge
[original_ledger_entry_id=...]``. This script checks each original debit and
writes only the remaining delta:

* under-reversed debit -> additional credit
* over-reversed debit -> correction debit

Dry-run by default.
"""

from __future__ import annotations

import argparse
from decimal import Decimal

from sqlalchemy import func

from app.db import SessionLocal
from app.models.billing import LedgerEntry, LedgerEntryType, LedgerSource
from app.services.common import round_money, to_decimal

BAD_DEBIT_PREFIX = "Prepaid charge: 30d"
REVERSAL_PREFIX = "Reversal of erroneous prepaid drawdown charge"
CORRECTION_PREFIX = "Correction of over-reversed prepaid drawdown charge"


def _memo(prefix: str, entry_id: object) -> str:
    return f"{prefix} [original_ledger_entry_id={entry_id}]"


def _sum_entries(db, *, account_id, entry_type, memo: str) -> Decimal:
    return round_money(
        to_decimal(
            db.query(func.coalesce(func.sum(LedgerEntry.amount), 0))
            .filter(LedgerEntry.account_id == account_id)
            .filter(LedgerEntry.entry_type == entry_type)
            .filter(LedgerEntry.memo == memo)
            .filter(LedgerEntry.is_active.is_(True))
            .scalar()
        )
    )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply", action="store_true", help="Write ledger deltas.")
    args = parser.parse_args()

    db = SessionLocal()
    under_count = 0
    over_count = 0
    under_total = Decimal("0.00")
    over_total = Decimal("0.00")
    try:
        bad_debits = (
            db.query(LedgerEntry)
            .filter(LedgerEntry.entry_type == LedgerEntryType.debit)
            .filter(LedgerEntry.memo.like(f"{BAD_DEBIT_PREFIX}%"))
            .filter(LedgerEntry.invoice_id.is_(None))
            .filter(LedgerEntry.is_active.is_(True))
            .order_by(LedgerEntry.created_at.asc())
            .all()
        )
        for debit in bad_debits:
            reversal_memo = _memo(REVERSAL_PREFIX, debit.id)
            correction_memo = _memo(CORRECTION_PREFIX, debit.id)
            reversed_amount = _sum_entries(
                db,
                account_id=debit.account_id,
                entry_type=LedgerEntryType.credit,
                memo=reversal_memo,
            )
            corrected_amount = _sum_entries(
                db,
                account_id=debit.account_id,
                entry_type=LedgerEntryType.debit,
                memo=correction_memo,
            )
            net_reversed = round_money(reversed_amount - corrected_amount)
            delta = round_money(round_money(debit.amount) - net_reversed)
            if delta > 0:
                under_count += 1
                under_total = round_money(under_total + delta)
                if args.apply:
                    db.add(
                        LedgerEntry(
                            account_id=debit.account_id,
                            invoice_id=None,
                            payment_id=None,
                            entry_type=LedgerEntryType.credit,
                            source=LedgerSource.adjustment,
                            amount=delta,
                            currency=debit.currency or "NGN",
                            memo=reversal_memo,
                        )
                    )
            elif delta < 0:
                amount = abs(delta)
                over_count += 1
                over_total = round_money(over_total + amount)
                if args.apply:
                    db.add(
                        LedgerEntry(
                            account_id=debit.account_id,
                            invoice_id=None,
                            payment_id=None,
                            entry_type=LedgerEntryType.debit,
                            source=LedgerSource.adjustment,
                            amount=amount,
                            currency=debit.currency or "NGN",
                            memo=correction_memo,
                        )
                    )

        if args.apply:
            db.commit()

        mode = "APPLY" if args.apply else "DRY-RUN"
        print(f"prepaid drawdown reversal reconcile — {mode}")
        print(f"bad_debits_scanned: {len(bad_debits)}")
        print(f"under_reversed_count: {under_count}")
        print(f"under_reversed_credit_needed: {under_total}")
        print(f"over_reversed_count: {over_count}")
        print(f"over_reversed_debit_needed: {over_total}")
    finally:
        db.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
