"""Soft-delete phantom opening-balance debit/reversal ledger pairs.

The cutover seed created phantom "Prepaid opening balance @ cutover" debits;
``reconcile_phantom_opening_debits`` later added compensating credits but left
both rows active for audit. Customers now see a charge plus a same-amount
reversal on their ledger and read it as a billing mistake. Every customer and
admin display surface filters ``LedgerEntry.is_active``, so flipping both rows
of each pair to inactive removes them from the UI while keeping the rows (and
their memos) queryable for audit. Each pair nets to zero, so balances are
unchanged.

Also retires the one legacy "excess reversal" noise pair: a reversal credit
that was itself corrected by an equal debit referencing it.

Dry-run by default; ``--apply`` to execute. Idempotent: already-inactive rows
are never selected again.

Examples
--------
  python -m scripts.one_off.soft_delete_phantom_opening_balance_pairs
  python -m scripts.one_off.soft_delete_phantom_opening_balance_pairs --apply
"""

from __future__ import annotations

import argparse
import csv
import re
import sys
from decimal import Decimal
from pathlib import Path
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db import SessionLocal
from app.models.billing import LedgerEntry, LedgerEntryType

OPENING_MEMO = "Prepaid opening balance @ cutover"
PHANTOM_REVERSAL_PREFIX = (
    "Reversal of phantom prepaid opening balance cutover debit [id="
)
LEGACY_REVERSAL_PREFIX = "Reversal of prepaid opening balance cutover debit [id="
EXCESS_CORRECTION_PREFIX = (
    "Correction: remove excess opening-balance reversal credit [id="
)
ID_RE = re.compile(r"\[id=([0-9a-f-]{36})\]")


def _referenced_id(memo: str | None) -> UUID | None:
    match = ID_RE.search(memo or "")
    return UUID(match.group(1)) if match else None


def _collect_pairs(
    session: Session,
) -> tuple[list[tuple[LedgerEntry, LedgerEntry]], list[str]]:
    """Return validated (reversal, counterpart) pairs plus skip reasons."""
    pairs: list[tuple[LedgerEntry, LedgerEntry]] = []
    skipped: list[str] = []

    reversals = (
        session.execute(
            select(LedgerEntry).where(
                LedgerEntry.is_active.is_(True),
                LedgerEntry.memo.like(f"{PHANTOM_REVERSAL_PREFIX}%"),
            )
        )
        .scalars()
        .all()
    )
    for rev in reversals:
        orig_id = _referenced_id(rev.memo)
        original = session.get(LedgerEntry, orig_id) if orig_id else None
        if original is None:
            skipped.append(f"{rev.id}: referenced original {orig_id} not found")
        elif not original.is_active:
            skipped.append(f"{rev.id}: original {original.id} already inactive")
        elif original.memo != OPENING_MEMO:
            skipped.append(f"{rev.id}: original memo mismatch ({original.memo!r})")
        elif original.entry_type is not LedgerEntryType.debit:
            skipped.append(f"{rev.id}: original {original.id} is not a debit")
        elif original.account_id != rev.account_id:
            skipped.append(f"{rev.id}: account mismatch with original {original.id}")
        elif original.amount != rev.amount:
            skipped.append(f"{rev.id}: amount mismatch with original {original.id}")
        elif original.invoice_id or original.payment_id:
            skipped.append(
                f"{rev.id}: original {original.id} linked to invoice/payment"
            )
        else:
            pairs.append((rev, original))

    # Legacy noise pair: an excess reversal credit cancelled by a correction
    # debit that references the credit's id. The opening debit itself was real
    # and stays active.
    corrections = (
        session.execute(
            select(LedgerEntry).where(
                LedgerEntry.is_active.is_(True),
                LedgerEntry.memo.like(f"{EXCESS_CORRECTION_PREFIX}%"),
            )
        )
        .scalars()
        .all()
    )
    for corr in corrections:
        rev_id = _referenced_id(corr.memo)
        rev = session.get(LedgerEntry, rev_id) if rev_id else None
        if rev is None or not rev.is_active:
            skipped.append(f"{corr.id}: referenced reversal {rev_id} missing/inactive")
        elif not (rev.memo or "").startswith(LEGACY_REVERSAL_PREFIX):
            skipped.append(
                f"{corr.id}: referenced row {rev.id} is not a legacy reversal"
            )
        elif rev.account_id != corr.account_id or rev.amount != corr.amount:
            skipped.append(f"{corr.id}: account/amount mismatch with reversal {rev.id}")
        else:
            pairs.append((corr, rev))

    return pairs, skipped


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply", action="store_true", help="write changes")
    parser.add_argument(
        "--csv", type=Path, default=None, help="write affected row ids to this CSV"
    )
    args = parser.parse_args()

    session = SessionLocal()
    try:
        pairs, skipped = _collect_pairs(session)
        total = sum((rev.amount for rev, _ in pairs), Decimal("0"))
        accounts = {rev.account_id for rev, _ in pairs}
        print(
            f"{'APPLY' if args.apply else 'DRY-RUN'}: {len(pairs)} pairs "
            f"({len(pairs) * 2} rows) across {len(accounts)} accounts, "
            f"pair amount total {total}"
        )
        for line in skipped:
            print(f"SKIP {line}")

        if args.csv:
            with args.csv.open("w", newline="") as fh:
                writer = csv.writer(fh)
                writer.writerow(["entry_id", "counterpart_id", "account_id", "amount"])
                for rev, other in pairs:
                    writer.writerow([rev.id, other.id, rev.account_id, rev.amount])
            print(f"wrote {args.csv}")

        if not args.apply:
            return 0

        for rev, other in pairs:
            rev.is_active = False
            other.is_active = False
        session.commit()
        print(f"deactivated {len(pairs) * 2} ledger rows")
        return 0
    finally:
        session.close()


if __name__ == "__main__":
    sys.exit(main())
