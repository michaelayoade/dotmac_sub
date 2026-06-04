"""Sync Subscription.status FROM Splynx services_internet → dotmac_sub.

Eliminates dotmac-native statuses (`suspended`, `archived`) that drifted from
Splynx during dual-run. Splynx is the source of truth for service lifecycle
state until Phase 5 (billing cutover).

Mapping (same as phase1):
    Splynx status            → dotmac SubscriptionStatus
    -----------------------  --------------------------------
    active                   → active
    disabled                 → disabled
    stopped                  → stopped
    hidden                   → hidden
    pending                  → pending
    (deleted=1 takes priority) → canceled

If a dotmac sub is currently `suspended` or `archived` (dotmac-native), it gets
re-mapped to whatever Splynx says.

Uses direct MySQL (not API) — much faster, one bulk query.

Usage:
    docker exec -e PYTHONPATH=/app -w /app dotmac_sub_app \\
        python -m scripts.migration.sync_subscription_status_from_splynx
    docker exec -e PYTHONPATH=/app -w /app dotmac_sub_app \\
        python -m scripts.migration.sync_subscription_status_from_splynx --execute
"""

from __future__ import annotations

import argparse
import logging
from collections import Counter

from sqlalchemy import select, update

from app.db import SessionLocal
from app.models.catalog import Subscription, SubscriptionStatus
from app.services.migrations.db_connections import splynx_connection

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger(__name__)

# Splynx services_internet.status → dotmac SubscriptionStatus
STATUS_MAP = {
    "active": SubscriptionStatus.active,
    "disabled": SubscriptionStatus.disabled,
    "stopped": SubscriptionStatus.stopped,
    "hidden": SubscriptionStatus.hidden,
    "pending": SubscriptionStatus.pending,
    "blocked": SubscriptionStatus.blocked,  # In case Splynx ever uses it
    "new": SubscriptionStatus.pending,
}


def run(dry_run: bool = True) -> dict[str, int]:
    db = SessionLocal()
    stats: Counter = Counter()
    transitions: Counter = Counter()  # (from → to) counts
    ip_changes: Counter = Counter()  # count of how many IP changes by kind
    not_in_splynx: list[str] = []

    try:
        # All subs with a splynx_service_id (we can sync these)
        subs = db.execute(
            select(
                Subscription.id,
                Subscription.splynx_service_id,
                Subscription.status,
                Subscription.login,
                Subscription.ipv4_address,
            ).where(Subscription.splynx_service_id.isnot(None))
        ).all()
        stats["dotmac_subs_with_splynx_id"] = len(subs)
        logger.info("dotmac subs with splynx_service_id: %d", len(subs))

        if not subs:
            return dict(stats)

        # Bulk fetch from Splynx — now also pull ipv4 and customer_id
        splynx_ids = [s.splynx_service_id for s in subs]
        with splynx_connection() as conn:
            with conn.cursor() as cur:
                ids_csv = ",".join(str(int(x)) for x in splynx_ids)
                cur.execute(
                    f"SELECT id, status, deleted, ipv4, customer_id "  # noqa: S608  # nosec B608
                    f"FROM services_internet WHERE id IN ({ids_csv})"
                )
                splynx_rows = cur.fetchall()

        splynx_lookup: dict[int, dict] = {row["id"]: row for row in splynx_rows}
        stats["splynx_rows_found"] = len(splynx_lookup)
        logger.info("matched %d Splynx services_internet rows", len(splynx_lookup))

        # Compute target status + IP per sub
        to_update: list[tuple] = []  # (sub, status_update_or_None, ipv4_update_or_None)
        for sub in subs:
            splynx_data = splynx_lookup.get(sub.splynx_service_id)
            if splynx_data is None:
                stats["missing_in_splynx"] += 1
                if len(not_in_splynx) < 10:
                    not_in_splynx.append(sub.login or str(sub.splynx_service_id))
                continue
            sx_status = (splynx_data["status"] or "active").strip().lower()
            sx_deleted = splynx_data["deleted"]
            sx_ipv4 = (splynx_data["ipv4"] or "").strip() or None

            # Target status
            if sx_deleted == "1":
                target_status = SubscriptionStatus.canceled
            else:
                target_status = STATUS_MAP.get(sx_status, SubscriptionStatus.active)
            status_change = target_status if sub.status != target_status else None
            if status_change is not None:
                transitions[(sub.status.value, target_status.value)] += 1

            # Target IP — only update if Splynx has a non-empty IP and it differs
            # (avoid wiping a known IP with NULL from Splynx)
            ip_change = None
            if sx_ipv4 and sx_ipv4 != (sub.ipv4_address or ""):
                ip_change = sx_ipv4
                # Tag by IP kind for visibility
                kind = "walled-garden" if sx_ipv4.startswith("10.") else "real"
                ip_changes[kind] += 1

            if status_change is not None or ip_change is not None:
                to_update.append((sub, status_change, ip_change))

        stats["would_transition"] = len(to_update)
        logger.info("subs needing status and/or IP update: %d", len(to_update))
        if transitions:
            logger.info("--- status transitions ---")
            for (from_s, to_s), n in transitions.most_common():
                logger.info("  %s → %s : %d", from_s, to_s, n)
        if ip_changes:
            logger.info("--- IP changes ---")
            for kind, n in ip_changes.most_common():
                logger.info("  → %s : %d", kind, n)

        if not_in_splynx:
            logger.info("first 10 logins missing in Splynx: %s", not_in_splynx)

        if dry_run:
            logger.info("DRY RUN — no DB writes. Pass --execute to apply.")
            return dict(stats)

        # Apply updates in batches
        updated = 0
        for sub, status_change, ip_change in to_update:
            values = {}
            if status_change is not None:
                values["status"] = status_change
            if ip_change is not None:
                values["ipv4_address"] = ip_change
            db.execute(
                update(Subscription).where(Subscription.id == sub.id).values(**values)
            )
            updated += 1
            if updated % 500 == 0:
                db.commit()
                logger.info("committed %d updates", updated)
        db.commit()
        stats["applied"] = updated
        logger.info("applied %d updates total", updated)
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
