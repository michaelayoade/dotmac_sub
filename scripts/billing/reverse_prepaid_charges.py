"""Reverse a day's prepaid drawdown charges (kill-switch recovery).

If the engine charges wrong on a given day, this credits back every prepaid
charge debit posted that day — restoring balances without corrupting the ledger
(charges are kept; an offsetting credit is added, so the audit trail is intact).

Pair with the kill switch: set ``billing_enabled=false`` to halt the engine,
then run this for the bad date. Idempotent: a charge already reversed is
skipped (the reversal credit references the original debit id). Dry-run default.

Usage:
    python scripts/billing/reverse_prepaid_charges.py --date 2026-07-01
    python scripts/billing/reverse_prepaid_charges.py --date 2026-07-01 --execute
"""

from __future__ import annotations

import sys
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal

from app.db import SessionLocal
from app.models.billing import (
    LedgerCategory,
    LedgerEntry,
    LedgerEntryType,
    LedgerSource,
)
from app.services.prepaid_billing import PREPAID_CHARGE_MEMO_PREFIX

REVERSAL_MEMO_PREFIX = "Reversal of prepaid charge"


def _arg(flag: str) -> str | None:
    return sys.argv[sys.argv.index(flag) + 1] if flag in sys.argv else None


def main(execute: bool, target: date) -> None:
    db = SessionLocal()
    # Half-open UTC day range — cross-dialect, avoids func.date() type issues.
    day_start = datetime(target.year, target.month, target.day, tzinfo=UTC)
    day_end = day_start + timedelta(days=1)
    try:
        # The day's prepaid charge debits.
        charges = (
            db.query(LedgerEntry)
            .filter(LedgerEntry.entry_type == LedgerEntryType.debit)
            .filter(LedgerEntry.is_active.is_(True))
            .filter(LedgerEntry.memo.like(f"{PREPAID_CHARGE_MEMO_PREFIX}%"))
            .filter(LedgerEntry.created_at >= day_start)
            .filter(LedgerEntry.created_at < day_end)
            .all()
        )
        # Already-reversed originals (idempotency): each reversal credit's memo
        # ends with "[id=<original debit id>]".
        reversed_ids = set()
        for (memo,) in db.query(LedgerEntry.memo).filter(
            LedgerEntry.memo.like(f"{REVERSAL_MEMO_PREFIX}%")
        ):
            if memo and "id=" in memo:
                reversed_ids.add(memo.rsplit("id=", 1)[-1].rstrip("]").strip())
        to_reverse = [c for c in charges if str(c.id) not in reversed_ids]
        total = sum((c.amount or Decimal("0")) for c in to_reverse)

        print(f"=== reverse prepaid charges for {target.isoformat()} ===")
        print(f"prepaid charge debits that day : {len(charges)}")
        print(f"already reversed (skipped)     : {len(charges) - len(to_reverse)}")
        print(f"to reverse                     : {len(to_reverse)}")
        print(f"  total credit-back            : NGN {total:,.2f}")

        if not execute:
            print("\nDRY-RUN — nothing changed. Re-run with --execute to reverse.")
            return

        reversed_n = 0
        for c in to_reverse:
            db.add(
                LedgerEntry(
                    account_id=c.account_id,
                    entry_type=LedgerEntryType.credit,
                    source=LedgerSource.adjustment,
                    category=LedgerCategory.internet_service,
                    amount=c.amount,
                    currency=c.currency,
                    memo=f"{REVERSAL_MEMO_PREFIX} {target.isoformat()} [id={c.id}]",
                )
            )
            reversed_n += 1
            if reversed_n % 500 == 0:
                db.commit()
        db.commit()
        print(
            f"\nDONE — reversed {reversed_n} charges (NGN {total:,.2f} credited back)."
        )
    finally:
        db.close()


if __name__ == "__main__":
    raw = _arg("--date")
    if not raw:
        print("ERROR: --date YYYY-MM-DD required")
        sys.exit(2)
    main(
        execute="--execute" in sys.argv,
        target=datetime.strptime(raw, "%Y-%m-%d").date(),
    )
