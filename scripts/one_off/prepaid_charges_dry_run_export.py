"""Dry-run export of the prepaid drawdown charges (daily / non-monthly prepaid).

READ-ONLY. Posts nothing, commits nothing. Produces the per-subscription list
the supervised charges-only cutover run will act on, so it can be reviewed and
signed off BEFORE ``run_prepaid_charges`` is ever run for real.

It uses the engine's own ``_due_prepaid_subscriptions`` (so it inherits the
exact due-set, including the monthly-cycle exclusion that keeps drawdown and
monthly invoicing mutually exclusive) and ``_period_charge`` (so the amounts
match what the real run would post). A charge is the recurring PRICE normalised
to the period; it does not read the deposit and never suspends.

Classifications mirror the engine loop:
  * initialise_no_charge — first sighting (next_billing_at is null): the run
    only sets the cadence one period out, posts NO charge.
  * skip_zero_price      — resolved charge <= 0 (e.g. the 54 zero-price subs).
  * chargeable           — a debit equal to ``charge`` would be posted.

The advisory ``available_balance`` column is computed via the collections
helper and MAY still reflect the stale Splynx deposit for un-migrated accounts;
it is for eyeballing only and has no bearing on the charge amount.

Examples
--------
  # Inside the app container:
  python -m scripts.one_off.prepaid_charges_dry_run_export
  python -m scripts.one_off.prepaid_charges_dry_run_export --out /tmp/prepaid_dryrun.csv
"""

from __future__ import annotations

import argparse
import csv
import sys
from datetime import UTC, datetime
from decimal import Decimal

from app.db import SessionLocal
from app.services.prepaid_billing import (
    _due_prepaid_subscriptions,
    _period_charge,
)

FIELDS = [
    "subscriber_id",
    "login",
    "offer",
    "billing_cycle",
    "next_billing_at",
    "first_sighting",
    "period_days",
    "charge",
    "currency",
    "classification",
    "available_balance_advisory",
]


def _available_balance(db, account_id) -> str:
    """Advisory only — may reflect the stale deposit for un-migrated accounts."""
    try:
        from app.services.collections import get_available_balance

        return str(get_available_balance(db, str(account_id)))
    except Exception:
        return ""


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--out",
        default="prepaid_charges_dry_run.csv",
        help="CSV output path (default: prepaid_charges_dry_run.csv)",
    )
    parser.add_argument(
        "--no-balance",
        action="store_true",
        help="Skip the advisory available_balance column (faster).",
    )
    args = parser.parse_args(argv)

    db = SessionLocal()
    now = datetime.now(UTC)
    try:
        due = _due_prepaid_subscriptions(db, now)

        rows: list[dict] = []
        n_charge = n_init = n_zero = 0
        total = Decimal("0.00")

        for sub in due:
            charge, currency, period_days = _period_charge(db, sub, now)
            first_sighting = sub.next_billing_at is None
            if first_sighting:
                classification = "initialise_no_charge"
                n_init += 1
            elif charge <= Decimal("0.00"):
                classification = "skip_zero_price"
                n_zero += 1
            else:
                classification = "chargeable"
                n_charge += 1
                total += charge

            offer = sub.offer
            rows.append(
                {
                    "subscriber_id": str(sub.subscriber_id),
                    "login": sub.login or "",
                    "offer": offer.name if offer else "",
                    "billing_cycle": (
                        offer.billing_cycle.value
                        if offer and offer.billing_cycle
                        else ""
                    ),
                    "next_billing_at": (
                        sub.next_billing_at.isoformat() if sub.next_billing_at else ""
                    ),
                    "first_sighting": first_sighting,
                    "period_days": period_days,
                    "charge": str(charge),
                    "currency": currency,
                    "classification": classification,
                    "available_balance_advisory": (
                        ""
                        if args.no_balance
                        else _available_balance(db, sub.subscriber_id)
                    ),
                }
            )

        with open(args.out, "w", newline="") as fh:
            writer = csv.DictWriter(fh, fieldnames=FIELDS)
            writer.writeheader()
            writer.writerows(rows)

        print(
            f"prepaid drawdown dry-run @ {now.isoformat()}  (READ-ONLY, nothing posted)"
        )
        print(f"  due (daily/non-monthly prepaid): {len(due)}")
        print(f"  chargeable now:                  {n_charge}   total ₦{total}")
        print(f"  initialise-only (no charge):     {n_init}")
        print(f"  skip zero-price:                 {n_zero}")
        print(f"  CSV written: {args.out}")
        return 0
    finally:
        db.close()


if __name__ == "__main__":
    sys.exit(main())
