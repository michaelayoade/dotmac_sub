"""Backfill subscriber_additional_routes from Splynx services_internet.ipv4_route.

Splynx auto-assigned additional IP blocks by attaching a Framed-Route to its
Access-Accept, driven by the ``ipv4_route`` field on each internet service.
That data lived only in Splynx and was never migrated, so after the
2026-06-11 RADIUS cutover (dotmac_sub became the answering server) every
additional IP silently stopped being routed.

This script pulls the active route-bearing services out of Splynx, maps each
to a dotmac_sub subscriber via ``subscribers.splynx_customer_id``, and upserts
the distinct ``(subscriber, CIDR)`` blocks into ``subscriber_additional_routes``.
The RADIUS reply builder reads that table and emits one Framed-Route per row.

Idempotent: the (subscriber_id, cidr) unique constraint plus an existence check
mean re-running only inserts genuinely new blocks. Designed to also be the body
of the incremental Splynx sync going forward.

Dry-run by default; pass --execute to write.

Usage:
    docker exec -e PYTHONPATH=/app -w /app dotmac_sub_app \\
        python -m scripts.migration.backfill_additional_routes
    docker exec -e PYTHONPATH=/app -w /app dotmac_sub_app \\
        python -m scripts.migration.backfill_additional_routes --execute
"""

from __future__ import annotations

import argparse
import ipaddress
import logging
from collections import Counter, defaultdict

from app.db import SessionLocal
from app.models.network import SubscriberAdditionalRoute
from app.models.subscriber import Subscriber, SubscriberStatus
from scripts.migration.db_connections import splynx_connection

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger(__name__)

SOURCE = "splynx_backfill"


def _normalise(token: str) -> tuple[str, int] | None:
    """Normalise a single ipv4_route token to (cidr, prefix_length).

    Bare hosts become /32. Returns None for blank/unparseable tokens.
    """
    token = token.strip()
    if not token:
        return None
    if "/" not in token:
        token = f"{token}/32"
    try:
        net = ipaddress.ip_network(token, strict=False)
    except ValueError:
        logger.warning("unparseable ipv4_route token: %r", token)
        return None
    return str(net), net.prefixlen


def _fetch_splynx_routes() -> list[dict]:
    """Active internet services that carry a non-empty ipv4_route."""
    with splynx_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT id, customer_id, ipv4, ipv4_route
                FROM services_internet
                WHERE status = 'active'
                  AND ipv4_route IS NOT NULL
                  AND TRIM(ipv4_route) <> ''
                ORDER BY customer_id, id
                """
            )
            return list(cur.fetchall())


def backfill(execute: bool) -> dict:
    services = _fetch_splynx_routes()

    # customer_id -> {cidr: (prefix_length, splynx_service_id)} — distinct blocks.
    # Splynx duplicate services collapse here; first service id wins for provenance.
    per_customer: dict[int, dict[str, tuple[int, int]]] = defaultdict(dict)
    raw_tokens = 0
    for svc in services:
        for tok in str(svc["ipv4_route"]).split(","):
            norm = _normalise(tok)
            if norm is None:
                continue
            raw_tokens += 1
            cidr, prefix = norm
            per_customer[svc["customer_id"]].setdefault(cidr, (prefix, svc["id"]))

    distinct_blocks = sum(len(v) for v in per_customer.values())
    customer_ids = list(per_customer.keys())

    summary = {
        "active_services": len(services),
        "raw_tokens": raw_tokens,
        "distinct_blocks": distinct_blocks,
        "customers": len(customer_ids),
        "unmapped_customers": 0,
        "blocked_routes": 0,
        "inserted": 0,
        "already_present": 0,
    }

    db = SessionLocal()
    try:
        sub_rows = (
            db.query(
                Subscriber.splynx_customer_id,
                Subscriber.id,
                Subscriber.status,
            )
            .filter(Subscriber.splynx_customer_id.in_(customer_ids))
            .all()
        )
        sub_by_cust = {r[0]: (r[1], r[2]) for r in sub_rows}

        status_spread: Counter = Counter()
        unmapped: list[int] = []

        for cust_id, blocks in per_customer.items():
            mapped = sub_by_cust.get(cust_id)
            if mapped is None:
                unmapped.append(cust_id)
                continue
            subscriber_id, status = mapped
            status_spread[status.value] += len(blocks)

            for cidr, (prefix, svc_id) in blocks.items():
                exists = (
                    db.query(SubscriberAdditionalRoute.id)
                    .filter(
                        SubscriberAdditionalRoute.subscriber_id == subscriber_id,
                        SubscriberAdditionalRoute.cidr == cidr,
                    )
                    .first()
                )
                if exists:
                    summary["already_present"] += 1
                    continue
                summary["inserted"] += 1
                if execute:
                    db.add(
                        SubscriberAdditionalRoute(
                            subscriber_id=subscriber_id,
                            cidr=cidr,
                            prefix_length=prefix,
                            metric=1,
                            is_active=True,
                            source=SOURCE,
                            splynx_service_id=svc_id,
                        )
                    )

        summary["unmapped_customers"] = len(unmapped)
        summary["unmapped_customer_ids"] = sorted(unmapped)
        summary["status_spread"] = dict(status_spread)
        # Routes that won't be emitted yet because the subscriber isn't active.
        summary["blocked_routes"] = sum(
            n for s, n in status_spread.items() if s != SubscriberStatus.active.value
        )

        if execute:
            db.commit()
            logger.info("committed %d new routes", summary["inserted"])
        else:
            db.rollback()
            logger.info("dry-run: no rows written (pass --execute to write)")
    finally:
        db.close()

    return summary


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--execute",
        action="store_true",
        help="Write rows. Without this flag the script only reports (dry-run).",
    )
    args = parser.parse_args()

    summary = backfill(execute=args.execute)

    print("\n=== additional-route backfill ===")
    print(f"  Splynx active route-bearing services : {summary['active_services']}")
    print(f"  raw route tokens                     : {summary['raw_tokens']}")
    print(f"  distinct (customer, CIDR) blocks     : {summary['distinct_blocks']}")
    print(f"  distinct customers                   : {summary['customers']}")
    print(f"  unmapped customers (no subscriber)   : {summary['unmapped_customers']}")
    if summary["unmapped_customer_ids"]:
        print(f"      -> {summary['unmapped_customer_ids']}")
    print(f"  routes by subscriber status          : {summary['status_spread']}")
    print(
        "  routes stored-but-not-emitted (non-active subs): "
        f"{summary['blocked_routes']}"
    )
    print(f"  would insert (new)                   : {summary['inserted']}")
    print(f"  already present (idempotent skip)    : {summary['already_present']}")
    print(
        f"  mode                                 : "
        f"{'EXECUTE (written)' if args.execute else 'DRY-RUN'}"
    )


if __name__ == "__main__":
    main()
