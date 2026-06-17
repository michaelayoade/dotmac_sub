"""Reverse erroneous credit-destroying debits from the cutover credit-settle bug.

The cutover credit-settle (``settle_open_invoices_from_credit``, run both by the
one-off backfill and the billing runner's inline step) wrote an offsetting
unallocated DEBIT for every naira it counted as "applied". But on invoices that
were already paid by a Splynx-synced allocation with a stale ``balance_due``,
``_apply_payment_allocation`` returned the EXISTING allocation's amount as
"applied" without creating a new allocation — so the settler debited real
unallocated credit while settling nothing new, destroying that credit.

This finds every account where the settle DEBITs (memo below) exceed the NEW
allocations the settle actually created (memo below), and reverses the excess
with a compensating CREDIT — append-only, exact, auditable. Correct settlements
(debit == new allocations) are untouched.

Dry-run by default (this IS the blast-radius report); nothing written without
--apply.

  python -m scripts.one_off.reverse_erroneous_credit_settle          # measure
  python -m scripts.one_off.reverse_erroneous_credit_settle --apply
"""

from __future__ import annotations

import argparse
from decimal import Decimal

from sqlalchemy import func

from app.db import SessionLocal
from app.models.billing import (
    LedgerEntry,
    LedgerEntryType,
    LedgerSource,
    Payment,
    PaymentAllocation,
)
from app.services.billing._common import get_account_credit_balance
from app.services.common import coerce_uuid, round_money, to_decimal

# Memos written by settle_open_invoices_from_credit.
SETTLE_DEBIT_MEMO = "Available balance applied to open invoices (cutover reconcile)"
SETTLE_ALLOC_MEMO = "Available balance applied (cutover reconcile)"
REVERSAL_MEMO = (
    "Reversal of erroneous cutover-reconcile debit "
    "(invoice already settled; no new credit was consumed)"
)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--apply",
        action="store_true",
        help="Write compensating credits. Without this flag the script is read-only.",
    )
    args = parser.parse_args()
    dry_run = not args.apply

    db = SessionLocal()
    total_wrongful = Decimal("0.00")
    rows: list[tuple[str, Decimal, Decimal, Decimal]] = []
    try:
        account_ids = [
            str(r[0])
            for r in (
                db.query(LedgerEntry.account_id)
                .filter(LedgerEntry.memo == SETTLE_DEBIT_MEMO)
                .filter(LedgerEntry.entry_type == LedgerEntryType.debit)
                .filter(LedgerEntry.is_active.is_(True))
                .distinct()
                .all()
            )
        ]
        for account_id in account_ids:
            aid = coerce_uuid(account_id)
            debit_sum = round_money(
                to_decimal(
                    db.query(func.coalesce(func.sum(LedgerEntry.amount), 0))
                    .filter(LedgerEntry.account_id == aid)
                    .filter(LedgerEntry.memo == SETTLE_DEBIT_MEMO)
                    .filter(LedgerEntry.entry_type == LedgerEntryType.debit)
                    .filter(LedgerEntry.is_active.is_(True))
                    .scalar()
                )
            )
            alloc_sum = round_money(
                to_decimal(
                    db.query(func.coalesce(func.sum(PaymentAllocation.amount), 0))
                    .join(Payment, Payment.id == PaymentAllocation.payment_id)
                    .filter(Payment.account_id == aid)
                    .filter(PaymentAllocation.memo == SETTLE_ALLOC_MEMO)
                    .filter(PaymentAllocation.is_active.is_(True))
                    .scalar()
                )
            )
            wrongful = round_money(debit_sum - alloc_sum)
            if wrongful <= 0:
                continue
            rows.append((account_id, debit_sum, alloc_sum, wrongful))
            total_wrongful = round_money(total_wrongful + wrongful)
            if args.apply:
                db.add(
                    LedgerEntry(
                        account_id=aid,
                        invoice_id=None,
                        payment_id=None,
                        entry_type=LedgerEntryType.credit,
                        source=LedgerSource.adjustment,
                        amount=wrongful,
                        currency="NGN",
                        memo=REVERSAL_MEMO,
                    )
                )
        if args.apply:
            db.commit()

        mode = "DRY-RUN (no changes written)" if dry_run else "APPLY"
        print(f"\n=== Reverse erroneous credit-settle debits — {mode} ===")
        print(f"accounts with wrongful debit : {len(rows)}")
        print(f"total credit to restore      : {total_wrongful}")
        if rows:
            print("\n--- per-account (debit / new-allocs / WRONGFUL) ---")
            for account_id, debit_sum, alloc_sum, wrongful in rows:
                tail = ""
                if args.apply:
                    tail = f"  credit_now={get_account_credit_balance(db, account_id)}"
                print(
                    f"  {account_id[:8]}: debit={debit_sum} allocs={alloc_sum} "
                    f"WRONGFUL={wrongful}{tail}"
                )
        if dry_run:
            print("\nRe-run with --apply to write the compensating credits.")
    finally:
        db.close()


if __name__ == "__main__":
    main()
