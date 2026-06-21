"""Re-provision RADIUS radreply for active subscribers that have additional routes.

After the additional-IP Framed-Route builder went live (PR #259), existing
``radreply`` rows do NOT carry the routes — they were written before the builder
was deployed. ``radreply`` is only (re)written at provision time, so each
affected subscription must be reconciled for its Framed-Route attributes to
appear; a PoD/re-auth then serves them.

Uses ``reconcile_subscription_connectivity`` (the working path). Note:
``sync_credential_to_radius`` silently no-ops here because the lone
``RadiusSyncJob`` has ``sync_users=False``.

Idempotent (delete+insert per user). Dry-run by default; ``--execute`` to write.

Usage:
    docker exec -w /app dotmac_sub_app python -m \
        scripts.one_off.reprovision_additional_route_subs            # dry-run
    docker exec -w /app dotmac_sub_app python -m \
        scripts.one_off.reprovision_additional_route_subs --execute  # write
    # optional: --exclude 100000158  (skip the already-done canary login)
"""

from __future__ import annotations

import argparse
import logging

from sqlalchemy import Column, String, func, select

from app.db import SessionLocal
from app.models.network import SubscriberAdditionalRoute
from app.models.subscriber import Subscriber, SubscriberStatus
from app.services.radius import (
    _active_external_sync_configs,
    _external_radius_table,
    _get_external_engine,
    _radius_sync_subscription_for_subscriber,
    reconcile_subscription_connectivity,
)

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger(__name__)


def _fleet_framed_route_count(db) -> tuple[int, int]:
    """(total Framed-Route rows, distinct usernames) across external radreply."""
    total = users = 0
    for cfg in _active_external_sync_configs(db):
        eng = _get_external_engine(cfg["db_url"])
        rr = _external_radius_table(
            cfg["radreply_table"],
            Column("username", String),
            Column("attribute", String),
            Column("op", String),
            Column("value", String),
        )
        with eng.connect() as ec:
            total += ec.execute(
                select(func.count())
                .select_from(rr)
                .where(rr.c.attribute == "Framed-Route")
            ).scalar()
            users += ec.execute(
                select(func.count(func.distinct(rr.c.username))).where(
                    rr.c.attribute == "Framed-Route"
                )
            ).scalar()
    return total, users


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--execute", action="store_true", help="Write (default: dry-run)"
    )
    parser.add_argument(
        "--exclude",
        action="append",
        default=[],
        help="PPPoE login(s) to skip (e.g. an already-done canary).",
    )
    args = parser.parse_args()

    db = SessionLocal()
    try:
        # Active subscribers that have at least one additional route.
        route_subs = {
            s for (s,) in db.query(SubscriberAdditionalRoute.subscriber_id).distinct()
        }
        active = list(
            db.scalars(
                select(Subscriber).where(
                    Subscriber.id.in_(route_subs),
                    Subscriber.status == SubscriberStatus.active,
                )
            )
        )
        logger.info("active subscribers with additional routes: %d", len(active))

        before_total, before_users = _fleet_framed_route_count(db)
        print("\n=== additional-route radreply reconcile ===")
        print(
            f"  fleet Framed-Route BEFORE: {before_total} rows / {before_users} users"
        )
        print(f"  mode: {'EXECUTE' if args.execute else 'DRY-RUN'}")
        print(f"  exclude logins: {args.exclude or '(none)'}\n")

        planned = skipped = done = failed = 0
        for sub in active:
            subscription = _radius_sync_subscription_for_subscriber(db, sub.id)
            n_routes = (
                db.query(func.count(SubscriberAdditionalRoute.id))
                .filter(
                    SubscriberAdditionalRoute.subscriber_id == sub.id,
                    SubscriberAdditionalRoute.is_active.is_(True),
                )
                .scalar()
            )

            login = getattr(subscription, "login", None) or str(sub.splynx_customer_id)
            if subscription is None:
                skipped += 1
                print(
                    f"  SKIP cust={sub.splynx_customer_id}: no sync-eligible subscription"
                )
                continue
            if login in args.exclude:
                skipped += 1
                print(f"  SKIP {login}: excluded")
                continue

            planned += 1
            if not args.execute:
                print(
                    f"  PLAN cust={sub.splynx_customer_id} sub={subscription.id} "
                    f"routes={n_routes}"
                )
                continue

            try:
                res = reconcile_subscription_connectivity(db, str(subscription.id))
                db.commit()
                done += 1
                print(
                    f"  OK   cust={sub.splynx_customer_id} sub={subscription.id} "
                    f"routes={n_routes} synced={res.get('external_credentials_synced')}"
                )
            except Exception as e:  # noqa: BLE001
                db.rollback()
                failed += 1
                print(
                    f"  FAIL cust={sub.splynx_customer_id} sub={subscription.id}: {e}"
                )

        after_total, after_users = _fleet_framed_route_count(db)
        print(
            f"\n  planned: {planned} | done: {done} | skipped: {skipped} | failed: {failed}"
        )
        print(f"  fleet Framed-Route AFTER: {after_total} rows / {after_users} users")
        if not args.execute:
            print("  (dry-run — no writes; re-run with --execute)")
    finally:
        db.close()


if __name__ == "__main__":
    main()
