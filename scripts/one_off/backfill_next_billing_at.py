"""Re-initialize subscriptions.next_billing_at from paid-through state.

Most active subscriptions carry a stale or NULL next_billing_at: the Splynx
migration backfill set it once from imported invoice periods and the local
invoice cycle never successfully advanced it (see billing_runs history). This
re-anchors the field so the billing runner — and the customer-facing
"days left" figure — starts from reality instead of trying to catch up on
months of pre-cutover history.

Per subscription (active, next_billing_at NULL or in the past):
  1. paid-through = latest active invoice line's billing_period_end, falling
     back to start_at, then created_at;
  2. roll forward one billing cycle at a time until the boundary lands after
     --forgive-before (default: now). Periods ending before that date are
     forgiven — they belong to the Splynx era and must not be re-billed here.

Pass --forgive-before 2026-05-18 (the local-payments cutover) to instead let
the runner catch up on post-cutover lapsed periods.

Dry-run by default; nothing is written without --apply.
"""

from __future__ import annotations

import argparse
from collections import Counter
from datetime import UTC, datetime

from sqlalchemy import func

from app.db import SessionLocal
from app.models.billing import Invoice, InvoiceLine
from app.models.catalog import BillingCycle, Subscription, SubscriptionStatus
from app.services.billing_automation import _as_utc, _period_end, _resolve_price

BATCH_SIZE = 500


def _paid_through_map(db) -> dict[str, datetime]:
    """Latest invoiced period end per subscription, in one query."""
    rows = (
        db.query(
            InvoiceLine.subscription_id,
            func.max(Invoice.billing_period_end),
        )
        .join(Invoice, Invoice.id == InvoiceLine.invoice_id)
        .filter(InvoiceLine.is_active.is_(True))
        .filter(Invoice.is_active.is_(True))
        .filter(Invoice.billing_period_end.isnot(None))
        .group_by(InvoiceLine.subscription_id)
        .all()
    )
    return {str(sub_id): _as_utc(end) for sub_id, end in rows if sub_id and end}


def compute_target(
    anchor: datetime, cycle: BillingCycle, forgive_before: datetime
) -> datetime:
    """First cycle boundary after `forgive_before`, anchored at `anchor`."""
    target = anchor
    # Bounded loop: even a decade of monthly cycles is only ~120 steps.
    for _ in range(2000):
        if target > forgive_before:
            return target
        target = _period_end(target, cycle)
    return target


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--apply",
        action="store_true",
        help="Write changes. Default is a dry run that only reports.",
    )
    parser.add_argument(
        "--forgive-before",
        type=lambda s: datetime.fromisoformat(s).replace(tzinfo=UTC),
        default=datetime.now(UTC),
        help=(
            "Periods ending before this date are skipped, not billed. "
            "Default: now (no catch-up billing at all)."
        ),
    )
    parser.add_argument("--limit", type=int, default=None, help="Cap rows (testing).")
    args = parser.parse_args()

    db = SessionLocal()
    now = datetime.now(UTC)
    stats: Counter = Counter()
    samples: list[str] = []
    try:
        paid_through = _paid_through_map(db)

        query = (
            db.query(Subscription)
            .filter(Subscription.status == SubscriptionStatus.active)
            .filter(
                (Subscription.next_billing_at.is_(None))
                | (Subscription.next_billing_at < now)
            )
            .order_by(Subscription.created_at)
        )
        if args.limit:
            query = query.limit(args.limit)

        # Materialize the candidate set up front: the batched commits below
        # would invalidate a yield_per server-side cursor mid-iteration.
        candidates = query.all()

        pending = 0
        for sub in candidates:
            stats["scanned"] += 1
            _, _, cycle = _resolve_price(db, sub)
            cycle = cycle or BillingCycle.monthly

            anchor = (
                paid_through.get(str(sub.id))
                or _as_utc(sub.start_at)
                or _as_utc(sub.created_at)
            )
            if anchor is None:
                stats["no_anchor"] += 1
                continue
            if paid_through.get(str(sub.id)):
                stats["anchored_on_invoice"] += 1
            else:
                stats["anchored_on_start"] += 1

            target = compute_target(anchor, cycle, args.forgive_before)
            old = _as_utc(sub.next_billing_at)
            if old == target:
                stats["already_correct"] += 1
                continue

            stats["updated"] += 1
            if len(samples) < 10:
                samples.append(
                    f"  {sub.id}  {old.date() if old else None} -> {target.date()}"
                    f"  (anchor {anchor.date()}, {cycle.value})"
                )
            if args.apply:
                sub.next_billing_at = target
                pending += 1
                if pending >= BATCH_SIZE:
                    db.commit()
                    pending = 0

        if args.apply and pending:
            db.commit()
    finally:
        db.close()

    mode = "APPLIED" if args.apply else "DRY RUN (no writes)"
    print(f"\n{mode} — forgive-before {args.forgive_before.date()}")
    for key in (
        "scanned",
        "updated",
        "already_correct",
        "anchored_on_invoice",
        "anchored_on_start",
        "no_anchor",
    ):
        print(f"  {key}: {stats[key]}")
    if samples:
        print("sample changes:")
        print("\n".join(samples))


if __name__ == "__main__":
    main()
