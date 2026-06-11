"""Shadow-reconcile local billing against Splynx (read-only).

While Splynx remains the billing system of record, this validates whether the
local invoice runner *would* bill each active subscriber the same recurring
amount Splynx actually charges — without writing anything. It is the evidence
needed to decide a billing cutover: if local billing reproduces Splynx within
tolerance across the base, the cutover is safe; the mismatches are the work
list to fix first.

For each active, Splynx-origin subscription it compares:
  local  = _resolve_price() recurring amount (what the runner would bill)
  splynx = the customer's MEDIAN Splynx invoice total (their typical monthly
           charge; median rather than latest to shrug off one-off proration
           and adjustment invoices)

and buckets the result:
  exact          local == splynx
  within_tol     |local - splynx| / splynx <= --tolerance (default 5%, the
                 band where tax/proration noise lives)
  mismatch       outside tolerance — a real pricing discrepancy to investigate
  no_local_price local has no resolvable recurring price
  no_splynx_ref  customer has no Splynx invoice history to compare against

Writes nothing. Safe to run against prod any time.
"""

from __future__ import annotations

import argparse
import statistics
from collections import Counter
from datetime import UTC, datetime
from decimal import Decimal

from sqlalchemy import select

from app.db import SessionLocal
from app.models.billing import Invoice
from app.models.catalog import Subscription, SubscriptionStatus
from app.models.subscriber import Subscriber, SubscriberStatus
from app.services.billing_automation import _effective_unit_price, _resolve_price


def _splynx_median_charge(db) -> dict[str, Decimal]:
    """Median Splynx invoice total per account (their typical monthly charge)."""
    rows = db.execute(
        select(Invoice.account_id, Invoice.total)
        .where(Invoice.splynx_invoice_id.is_not(None))
        .where(Invoice.total > Decimal("0.00"))
    ).all()
    by_account: dict[str, list[float]] = {}
    for account_id, total in rows:
        by_account.setdefault(str(account_id), []).append(float(total))
    return {
        acct: Decimal(str(round(statistics.median(vals), 2)))
        for acct, vals in by_account.items()
        if vals
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--tolerance",
        type=float,
        default=0.05,
        help="Fractional band treated as a match (default 0.05 = 5%%).",
    )
    parser.add_argument(
        "--sample",
        type=int,
        default=25,
        help="How many mismatch examples to print.",
    )
    parser.add_argument(
        "--csv",
        metavar="PATH",
        default=None,
        help=(
            "Write every mismatch to a CSV worklist (subscription, account, "
            "local price, Splynx median, drift) for the billing team to "
            "reconcile against each customer's current Splynx service price."
        ),
    )
    args = parser.parse_args()

    db = SessionLocal()
    stats: Counter = Counter()
    mismatches: list[str] = []
    worklist: list[tuple] = []
    try:
        splynx_med = _splynx_median_charge(db)

        subs = (
            db.query(Subscription)
            .join(Subscriber, Subscriber.id == Subscription.subscriber_id)
            .filter(Subscription.status == SubscriptionStatus.active)
            .filter(Subscriber.status == SubscriberStatus.active)
            .filter(Subscription.splynx_service_id.is_not(None))
            .all()
        )
        stats["scanned"] = len(subs)

        now = datetime.now(UTC)
        for sub in subs:
            catalog_amount, _currency, _cycle = _resolve_price(db, sub)
            if catalog_amount is None:
                stats["no_local_price"] += 1
                continue
            # Use the real billing price: per-subscriber negotiated unit_price
            # override + any active discount, exactly as run_invoice_cycle bills.
            amount = _effective_unit_price(sub, Decimal(str(catalog_amount)), now)
            ref = splynx_med.get(str(sub.subscriber_id))
            if ref is None or ref == 0:
                stats["no_splynx_ref"] += 1
                continue

            local = Decimal(str(amount))
            if local == ref:
                stats["exact"] += 1
                continue
            drift = abs(local - ref) / ref
            if drift <= Decimal(str(args.tolerance)):
                stats["within_tol"] += 1
            else:
                stats["mismatch"] += 1
                worklist.append(
                    (str(sub.id), str(sub.subscriber_id), local, ref, drift)
                )
                if len(mismatches) < args.sample:
                    mismatches.append(
                        f"  sub={sub.id} acct={sub.subscriber_id} "
                        f"local={local} splynx_median={ref} "
                        f"drift={drift:.1%}"
                    )
    finally:
        db.close()

    if args.csv and worklist:
        import csv

        with open(args.csv, "w", newline="") as fh:
            w = csv.writer(fh)
            w.writerow(
                [
                    "subscription_id",
                    "account_id",
                    "local_price",
                    "splynx_median",
                    "drift",
                ]
            )
            for sid, acct, local, ref, drift in worklist:
                w.writerow([sid, acct, local, ref, f"{drift:.4f}"])
        print(f"\n  wrote {len(worklist)} mismatches to {args.csv}")

    comparable = stats["exact"] + stats["within_tol"] + stats["mismatch"]
    print("\n=== shadow_reconcile_billing (READ ONLY) ===")
    for k in (
        "scanned",
        "exact",
        "within_tol",
        "mismatch",
        "no_local_price",
        "no_splynx_ref",
    ):
        print(f"  {k}: {stats[k]}")
    if comparable:
        agree = stats["exact"] + stats["within_tol"]
        print(
            f"\n  local matches Splynx (exact or within "
            f"{args.tolerance:.0%}): {agree}/{comparable} = "
            f"{agree / comparable:.1%} of comparable subscriptions"
        )
    if mismatches:
        print(f"\n  sample mismatches (first {len(mismatches)}):")
        print("\n".join(mismatches))


if __name__ == "__main__":
    main()
