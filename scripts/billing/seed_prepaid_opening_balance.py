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

from app.db import SessionLocal
from app.models.billing import (
    LedgerCategory,
    LedgerEntry,
    LedgerEntryType,
    LedgerSource,
)
from app.models.catalog import BillingMode
from app.models.subscriber import Subscriber
from app.services.prepaid_billing import PREPAID_OPENING_BALANCE_MEMO


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
        positive = [s for s in to_seed if Decimal(str(s.deposit)) > 0]
        negative = [s for s in to_seed if Decimal(str(s.deposit)) < 0]
        zero = [s for s in to_seed if Decimal(str(s.deposit)) == 0]
        credit_total = sum(Decimal(str(s.deposit)) for s in positive)
        debit_total = sum(-Decimal(str(s.deposit)) for s in negative)

        print("=== prepaid opening-balance seed ===")
        print(f"prepaid splynx-linked subscribers : {len(subs)}")
        print(f"already seeded (skipped)          : {len(subs) - len(to_seed)}")
        print(f"to seed, positive deposit (credit): {len(positive)}")
        print(f"  total opening credit            : NGN {credit_total:,.2f}")
        print(f"to seed, negative deposit (debit) : {len(negative)}")
        print(f"  total opening arrears (debit)   : NGN {debit_total:,.2f}")
        print(f"to seed, zero deposit (marker)    : {len(zero)}")

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
        # Positive deposit -> opening credit.
        for sub in positive:
            _seed(sub, LedgerEntryType.credit, Decimal(str(sub.deposit)))
            seeded += 1
        # Negative deposit -> opening debit (arrears preserved as a negative
        # ledger balance once the account switches to the ledger).
        for sub in negative:
            _seed(sub, LedgerEntryType.debit, -Decimal(str(sub.deposit)))
            seeded += 1
        # Zero deposit -> zero-amount credit marker, purely to flip the resolver.
        for sub in zero:
            _seed(sub, LedgerEntryType.credit, Decimal("0.00"))
            seeded += 1
        db.commit()
        print(
            f"\nDONE — seeded {seeded} accounts "
            f"({len(positive)} credit / {len(negative)} debit / {len(zero)} marker)."
        )
    finally:
        db.close()


if __name__ == "__main__":
    main(execute="--execute" in sys.argv)
