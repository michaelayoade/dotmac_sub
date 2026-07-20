"""Retire ledger noise from failed/voided payments and their reversals.

Cutover-era duplicate payments (Paystack recovery duplicates, rows later
voided in Splynx) were corrected with refund-source ledger debits, but both
the erroneous payment credit and the correcting debit were left active, so
customers see a charge-and-reversal pair on their portal ledger.

Two actions, both idempotent and dry-run by default:

1. PAIRS — for each active refund-source debit whose payment is failed or
   canceled and which has a matching active payment-source credit (same
   payment_id, account, and amount): deactivate both rows. Each pair nets to
   zero, so balances are unchanged.
2. LONE DEBITS — refund debits on failed/canceled payments with no matching
   active credit (the erroneous value arrived via the opening-balance seed,
   so the debit is load-bearing and must stay): replace the staff-jargon memo
   ("Voided in Splynx (system of record) — mirroring source deletion ...")
   with a customer-readable one. Old memos are written to the CSV audit.

Examples
--------
  python -m scripts.one_off.soft_delete_failed_payment_reversal_pairs
  python -m scripts.one_off.soft_delete_failed_payment_reversal_pairs --apply
"""

from __future__ import annotations

import argparse
import csv
import sys
from decimal import Decimal
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db import SessionLocal
from app.models.billing import (
    LedgerEntry,
    LedgerEntryType,
    LedgerSource,
    Payment,
    PaymentStatus,
)

DEAD_PAYMENT_STATUSES = (PaymentStatus.failed, PaymentStatus.canceled)
REWORDED_MEMO = "Reversal of duplicate payment (original payment voided at source)"


def _dead_payment_refund_debits(session: Session) -> list[LedgerEntry]:
    return (
        session.execute(
            select(LedgerEntry)
            .join(Payment, Payment.id == LedgerEntry.payment_id)
            .where(
                LedgerEntry.is_active.is_(True),
                LedgerEntry.entry_type == LedgerEntryType.debit,
                LedgerEntry.source == LedgerSource.refund,
                Payment.status.in_(DEAD_PAYMENT_STATUSES),
            )
        )
        .scalars()
        .all()
    )


def _matching_credit(session: Session, debit: LedgerEntry) -> LedgerEntry | None:
    credits = (
        session.execute(
            select(LedgerEntry).where(
                LedgerEntry.is_active.is_(True),
                LedgerEntry.payment_id == debit.payment_id,
                LedgerEntry.entry_type == LedgerEntryType.credit,
                LedgerEntry.source == LedgerSource.payment,
            )
        )
        .scalars()
        .all()
    )
    if len(credits) != 1:
        return None
    credit = credits[0]
    if credit.account_id != debit.account_id or credit.amount != debit.amount:
        return None
    return credit


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply", action="store_true", help="write changes")
    parser.add_argument(
        "--csv", type=Path, default=None, help="write affected rows to this CSV"
    )
    args = parser.parse_args()

    session = SessionLocal()
    try:
        pairs: list[tuple[LedgerEntry, LedgerEntry]] = []
        lone_debits: list[LedgerEntry] = []
        for debit in _dead_payment_refund_debits(session):
            credit = _matching_credit(session, debit)
            if credit is not None:
                pairs.append((debit, credit))
            else:
                lone_debits.append(debit)

        pair_total = sum((d.amount for d, _ in pairs), Decimal("0"))
        print(
            f"{'APPLY' if args.apply else 'DRY-RUN'}: {len(pairs)} net-zero pairs "
            f"({len(pairs) * 2} rows, total {pair_total}) to deactivate; "
            f"{len(lone_debits)} lone balance-bearing debits to reword"
        )
        already_worded = [d for d in lone_debits if d.memo == REWORDED_MEMO]
        lone_debits = [d for d in lone_debits if d.memo != REWORDED_MEMO]
        if already_worded:
            print(f"  ({len(already_worded)} lone debits already reworded — skipped)")

        if args.csv:
            with args.csv.open("w", newline="") as fh:
                writer = csv.writer(fh)
                writer.writerow(
                    [
                        "action",
                        "entry_id",
                        "counterpart_id",
                        "account_id",
                        "amount",
                        "old_memo",
                    ]
                )
                for debit, credit in pairs:
                    writer.writerow(
                        [
                            "deactivate_pair",
                            debit.id,
                            credit.id,
                            debit.account_id,
                            debit.amount,
                            debit.memo,
                        ]
                    )
                for debit in lone_debits:
                    writer.writerow(
                        [
                            "reword_memo",
                            debit.id,
                            "",
                            debit.account_id,
                            debit.amount,
                            debit.memo,
                        ]
                    )
            print(f"wrote {args.csv}")

        if not args.apply:
            return 0

        for debit, credit in pairs:
            debit.is_active = False
            credit.is_active = False
        for debit in lone_debits:
            debit.memo = REWORDED_MEMO
        session.commit()
        print(f"deactivated {len(pairs) * 2} rows, reworded {len(lone_debits)} memos")
        return 0
    finally:
        session.close()


if __name__ == "__main__":
    sys.exit(main())
