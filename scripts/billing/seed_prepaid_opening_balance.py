"""Seed prepaid opening balances into the AR ledger at cutover.

The prepaid drawdown engine makes the local AR ledger authoritative. This
one-time step posts, per prepaid Splynx-linked subscriber, a credit
``LedgerEntry`` equal to the subscriber's synced ``deposit`` (the
Splynx-authoritative balance at the cutover instant). After it runs, the
ledger balance equals the deposit and ``_resolve_prepaid_available_balance``
switches that account from the deposit to the ledger (the seed's memo is the
switch — see PREPAID_OPENING_BALANCE_MEMO).

Run AFTER a final ``resync_prepaid_deposits.py --execute`` and BEFORE enabling
``billing_enabled`` (see docs/designs/PREPAID_DRAWDOWN_ENGINE.md). Idempotent:
skips accounts already seeded. Dry-run by default.

Usage:
    python scripts/billing/seed_prepaid_opening_balance.py            # dry-run
    python scripts/billing/seed_prepaid_opening_balance.py --execute
"""

from __future__ import annotations

import sys
from decimal import Decimal

from sqlalchemy import case, func

from app.db import SessionLocal
from app.models.billing import (
    Invoice,
    InvoiceStatus,
    LedgerCategory,
    LedgerEntry,
    LedgerEntryType,
    LedgerSource,
)
from app.models.catalog import BillingMode
from app.models.subscriber import Subscriber
from app.services.prepaid_billing import PREPAID_OPENING_BALANCE_MEMO

# Below this, treat the true-up as zero and seed a flip-only marker.
_TOL = Decimal("0.01")


def main(execute: bool) -> None:
    db = SessionLocal()
    try:
        subs = (
            db.query(Subscriber)
            .filter(Subscriber.billing_mode == BillingMode.prepaid)
            .filter(Subscriber.splynx_customer_id.isnot(None))
            .filter(Subscriber.deposit.isnot(None))
            .all()
        )
        # Already-seeded accounts (idempotency).
        seeded_ids = {
            r[0]
            for r in db.query(LedgerEntry.account_id)
            .filter(LedgerEntry.entry_type == LedgerEntryType.credit)
            .filter(LedgerEntry.memo == PREPAID_OPENING_BALANCE_MEMO)
            .filter(LedgerEntry.is_active.is_(True))
            .all()
        }

        to_seed = [s for s in subs if s.id not in seeded_ids]

        # The post-seed balance the resolver reports is the ledger model:
        #   (Σ unallocated active credits − Σ unallocated active debits)
        #     − Σ open-invoice balance_due
        # The migrated ledger already carries this, and for ~83% of accounts it
        # ALREADY equals the deposit. So the seed must post the TRUE-UP delta
        #   delta = deposit − existing_ledger_net
        # (not the full deposit, which would double the already-correct accounts).
        # Post-seed the resolver then lands exactly on the Splynx deposit.
        signed = func.sum(
            case(
                (LedgerEntry.entry_type == LedgerEntryType.credit, LedgerEntry.amount),
                else_=-LedgerEntry.amount,
            )
        )
        ledger_net = {
            aid: Decimal(str(net or 0))
            for aid, net in db.query(LedgerEntry.account_id, signed)
            .filter(LedgerEntry.invoice_id.is_(None))
            .filter(LedgerEntry.is_active.is_(True))
            .group_by(LedgerEntry.account_id)
            .all()
        }
        open_inv = {
            aid: Decimal(str(bal or 0))
            for aid, bal in db.query(
                Invoice.account_id, func.sum(Invoice.balance_due)
            )
            .filter(Invoice.is_active.is_(True))
            .filter(
                Invoice.status.in_(
                    [
                        InvoiceStatus.issued,
                        InvoiceStatus.partially_paid,
                        InvoiceStatus.overdue,
                    ]
                )
            )
            .group_by(Invoice.account_id)
            .all()
        }

        plan: list[tuple] = []  # (sub, signed_delta)
        for s in to_seed:
            existing = ledger_net.get(s.id, Decimal("0")) - open_inv.get(
                s.id, Decimal("0")
            )
            delta = Decimal(str(s.deposit)) - existing
            plan.append((s, delta))
        credits = [(s, d) for s, d in plan if d > _TOL]
        debits = [(s, d) for s, d in plan if d < -_TOL]
        markers = [(s, d) for s, d in plan if -_TOL <= d <= _TOL]
        credit_total = sum(d for _, d in credits)
        debit_total = sum(-d for _, d in debits)

        print("=== prepaid opening-balance seed (true-up to Splynx deposit) ===")
        print(f"prepaid splynx-linked subscribers : {len(subs)}")
        print(f"already seeded (skipped)          : {len(subs) - len(to_seed)}")
        print(f"already on deposit (marker only)  : {len(markers)}")
        print(f"true-up credit (ledger < deposit) : {len(credits)}")
        print(f"  total credit delta              : NGN {credit_total:,.2f}")
        print(f"true-up debit  (ledger > deposit) : {len(debits)}")
        print(f"  total debit delta               : NGN {debit_total:,.2f}")

        if not execute:
            print("\nDRY-RUN — nothing changed. Re-run with --execute to seed.")
            return

        def _seed(sub, entry_type, amount):
            db.add(
                LedgerEntry(
                    account_id=sub.id,
                    entry_type=entry_type,
                    source=LedgerSource.adjustment,
                    category=LedgerCategory.deposit,
                    amount=amount,
                    currency="NGN",
                    memo=PREPAID_OPENING_BALANCE_MEMO,
                )
            )

        seeded = 0
        for sub, delta in credits:
            _seed(sub, LedgerEntryType.credit, delta.quantize(_TOL))
            seeded += 1
        for sub, delta in debits:
            _seed(sub, LedgerEntryType.debit, (-delta).quantize(_TOL))
            seeded += 1
        # Already-correct accounts: a zero-amount marker just flips the resolver.
        for sub, _delta in markers:
            _seed(sub, LedgerEntryType.credit, Decimal("0.00"))
            seeded += 1
        db.commit()
        print(
            f"\nDONE — seeded {seeded} accounts "
            f"({len(credits)} credit / {len(debits)} debit / {len(markers)} marker)."
        )
    finally:
        db.close()


if __name__ == "__main__":
    main(execute="--execute" in sys.argv)
