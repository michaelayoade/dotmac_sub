"""Sync Subscriber.status FROM Splynx customers → dotmac_sub.

Splynx's customer-level block (subscribers.status='blocked') is what triggers
walled-garden RADIUS treatment in Splynx_radd — not service-level status. This
script keeps dotmac_sub's Subscriber.status in lock-step with Splynx's
customers.status during dual-run.

Maps Splynx customers.status → dotmac SubscriberStatus (1:1 for the 4 values
Splynx uses).

Usage:
    docker exec -e PYTHONPATH=/app -w /app dotmac_sub_app \\
        python -m scripts.migration.sync_subscriber_status_from_splynx
    docker exec -e PYTHONPATH=/app -w /app dotmac_sub_app \\
        python -m scripts.migration.sync_subscriber_status_from_splynx --execute
"""

from __future__ import annotations

import argparse
import logging
from collections import Counter

from sqlalchemy import select

from app.db import SessionLocal
from app.models.subscriber import Subscriber, SubscriberStatus
from scripts.migration.db_connections import splynx_connection

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger(__name__)

STATUS_MAP = {
    "active": SubscriberStatus.active,
    "blocked": SubscriberStatus.blocked,
    "disabled": SubscriberStatus.disabled,
    "new": SubscriberStatus.new,
}


def run(dry_run: bool = True) -> dict[str, int]:
    db = SessionLocal()
    stats: Counter = Counter()
    transitions: Counter = Counter()
    not_in_splynx: list[int] = []

    try:
        subs = db.execute(
            select(
                Subscriber.id,
                Subscriber.splynx_customer_id,
                Subscriber.status,
                Subscriber.email,
            ).where(Subscriber.splynx_customer_id.isnot(None))
        ).all()
        stats["dotmac_subscribers_with_splynx_id"] = len(subs)
        logger.info("dotmac subscribers with splynx_customer_id: %d", len(subs))

        if not subs:
            return dict(stats)

        # Bulk fetch from Splynx customers
        splynx_ids = [s.splynx_customer_id for s in subs]
        with splynx_connection() as conn:
            with conn.cursor() as cur:
                ids_csv = ",".join(str(int(x)) for x in splynx_ids)
                cur.execute(
                    f"SELECT id, status, deleted FROM customers WHERE id IN ({ids_csv})"  # noqa: S608
                )
                splynx_rows = cur.fetchall()
        splynx_lookup: dict[int, dict] = {row["id"]: row for row in splynx_rows}
        stats["splynx_rows_found"] = len(splynx_lookup)

        # Determine dotmac "canceled" sentinel — what we use for Splynx deleted=1.
        # SubscriberStatus likely has 'canceled' or 'deleted' or similar; fall back
        # to disabled if missing.
        canceled_sentinel = getattr(
            SubscriberStatus,
            "canceled",
            getattr(SubscriberStatus, "deleted", SubscriberStatus.disabled),
        )

        to_update = []
        for sub in subs:
            splynx_row = splynx_lookup.get(sub.splynx_customer_id)
            if splynx_row is None:
                stats["missing_in_splynx"] += 1
                if len(not_in_splynx) < 10:
                    not_in_splynx.append(sub.splynx_customer_id)
                continue

            # CRITICAL: Splynx soft-delete (deleted=1) is independent of status.
            # Map deleted=1 to canceled regardless of what status field says.
            if splynx_row["deleted"] == "1":
                target = canceled_sentinel
            else:
                sx_status = (splynx_row["status"] or "active").strip().lower()
                target = STATUS_MAP.get(sx_status, SubscriberStatus.active)

            if sub.status != target:
                transitions[(sub.status.value, target.value)] += 1
                to_update.append((sub, target))

        stats["would_transition"] = len(to_update)
        logger.info("subscribers needing status update: %d", len(to_update))
        for (from_s, to_s), n in transitions.most_common():
            logger.info("  %s → %s : %d", from_s, to_s, n)
        if not_in_splynx:
            logger.info(
                "first 10 splynx_customer_ids missing in Splynx: %s", not_in_splynx
            )

        if dry_run:
            logger.info("DRY RUN — no DB writes. Pass --execute to apply.")
            return dict(stats)

        updated = 0
        for sub, target in to_update:
            db.execute(
                Subscriber.__table__.update()
                .where(Subscriber.id == sub.id)
                .values(status=target)
            )
            updated += 1
            if updated % 500 == 0:
                db.commit()
                logger.info("committed %d updates", updated)
        db.commit()
        stats["applied"] = updated
        logger.info("applied %d updates", updated)
    finally:
        db.close()

    return dict(stats)


if __name__ == "__main__":
    p = argparse.ArgumentParser()
    p.add_argument(
        "--execute", action="store_true", help="Apply updates (default dry-run)"
    )
    args = p.parse_args()
    run(dry_run=not args.execute)
