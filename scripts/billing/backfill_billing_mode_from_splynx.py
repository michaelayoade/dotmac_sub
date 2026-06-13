"""Backfill billing_mode from the authoritative Splynx ``customers.billing_type``.

The migration derived ``billing_mode`` from a billing_type field that does not
exist on ``services_internet`` (and never set the account-level mode at all),
so the entire base defaulted to prepaid. The true billing type lives in
Splynx ``customers.billing_type`` (``prepaid_monthly`` / ``prepaid`` /
``recurring``); only ``recurring`` is postpaid.

This realigns BOTH levels for splynx-linked subscribers:
  - ``subscriber.billing_mode``      (account level — new subs inherit it)
  - ``subscription.billing_mode``    (what prepaid/postpaid enforcement reads)

Safe by construction: dry-run by default; reads Splynx as the source of truth;
touches only billing_mode. ``billing_mode`` drives nothing while
``billing_enabled`` is False, so this is cutover-correctness prep.

Usage:
    python scripts/billing/backfill_billing_mode_from_splynx.py            # dry-run
    python scripts/billing/backfill_billing_mode_from_splynx.py --execute
"""

from __future__ import annotations

import sys
from collections import Counter

from app.db import SessionLocal
from app.models.catalog import Subscription
from app.models.subscriber import Subscriber
from app.services.migrations.billing_modes import map_billing_mode
from scripts.migration.db_connections import fetch_all, splynx_connection


def main(execute: bool) -> None:
    db = SessionLocal()
    try:
        # Local splynx-linked subscribers, keyed by Splynx customer id.
        subs = (
            db.query(Subscriber).filter(Subscriber.splynx_customer_id.isnot(None)).all()
        )
        by_cid = {int(s.splynx_customer_id): s for s in subs}
        print(f"splynx-linked subscribers: {len(by_cid)}")
        if not by_cid:
            return

        # Authoritative billing type from Splynx.
        with splynx_connection() as conn:
            rows = fetch_all(conn, "SELECT id, billing_type FROM customers")
        splynx_type = {int(r["id"]): (r.get("billing_type") or "") for r in rows}

        sub_fix = []  # (subscriber, current_mode, target_mode)
        for cid, sub in by_cid.items():
            raw = splynx_type.get(cid)
            if raw is None:
                continue
            target = map_billing_mode(raw)
            if sub.billing_mode != target:
                sub_fix.append((sub, sub.billing_mode, target))

        # Subscriptions whose mode disagrees with the customer's true mode.
        subscriber_ids = [s.id for s in by_cid.values()]
        all_subscriptions = (
            db.query(Subscription)
            .filter(Subscription.subscriber_id.in_(subscriber_ids))
            .all()
        )
        # Map subscriber_id -> target mode (only where we have a Splynx type).
        target_by_sub_id = {}
        for cid, sub in by_cid.items():
            raw = splynx_type.get(cid)
            if raw is not None:
                target_by_sub_id[sub.id] = map_billing_mode(raw)

        subscription_fix = [
            (sn, sn.billing_mode, target_by_sub_id[sn.subscriber_id])
            for sn in all_subscriptions
            if sn.subscriber_id in target_by_sub_id
            and sn.billing_mode != target_by_sub_id[sn.subscriber_id]
        ]

        def _summary(pairs):
            c = Counter(
                f"{cur.value if cur else None}->{tgt.value}" for _, cur, tgt in pairs
            )
            return ", ".join(f"{k}: {v}" for k, v in c.most_common())

        print("=== billing_mode backfill (Splynx customers.billing_type) ===")
        print(f"subscribers to fix   : {len(sub_fix)}  ({_summary(sub_fix)})")
        print(
            f"subscriptions to fix : {len(subscription_fix)}  "
            f"({_summary(subscription_fix)})"
        )

        if not execute:
            print("\nDRY-RUN — nothing changed. Re-run with --execute to apply.")
            return

        for sub, _, target in sub_fix:
            sub.billing_mode = target
        for sn, _, target in subscription_fix:
            sn.billing_mode = target
        db.commit()
        print(
            f"\nDONE — updated {len(sub_fix)} subscribers and "
            f"{len(subscription_fix)} subscriptions."
        )
    finally:
        db.close()


if __name__ == "__main__":
    main(execute="--execute" in sys.argv)
