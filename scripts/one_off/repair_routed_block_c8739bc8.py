#!/usr/bin/env python
"""Targeted repair: restore the routed /30 for subscriber c8739bc8 (dry-run first).

Field report: ``160.119.126.160/30`` "not in the list / all free", customer's
routed devices offline. Root cause (verified 2026-06-18): the /30 is modelled as
a ``SubscriberAdditionalRoute`` that is **inactive**, so the RADIUS sweep emits
no ``Framed-Route`` for it; ``.161/.162`` are stale duplicate ``wan``
IPAssignments (NOT primaries). The primary ``160.119.126.18`` is already correct.

This is a one-customer DATA repair, not a code path. It is assertion-heavy: if
prod state does not EXACTLY match the verified shape, it aborts and writes
nothing. ``ip_assignments.ipv4_address_id`` is unique + NOT NULL, so the stale
duplicate assignments are DELETED (they cannot be cleanly detached by nulling).

Usage (inside the app container; PYTHONPATH=/app):
    docker compose exec -T -e PYTHONPATH=/app app \
        python scripts/one_off/repair_routed_block_c8739bc8.py            # dry-run
    docker compose exec -T -e PYTHONPATH=/app app \
        python scripts/one_off/repair_routed_block_c8739bc8.py --apply
"""

import argparse
import ipaddress
import sys

from app.db import SessionLocal
from app.models.catalog import Subscription
from app.models.network import (
    IPAssignment,
    IPv4Address,
    SubscriberAdditionalRoute,
)
from app.models.subscriber import Subscriber
from app.services.common import coerce_uuid

SUBSCRIBER_ID = "c8739bc8-cd96-4e2e-86f3-b162d3e155cc"
EXPECTED_LOGIN = "100025880"
ROUTE_CIDR = "160.119.126.160/30"
# The /30's member hosts that exist as stale duplicate primary `wan` assignments.
STALE_DUP_IPS = ("160.119.126.161", "160.119.126.162")
# The whole /30 is routed to this customer — reserve every member so on-demand
# allocation never hands a piece of it to another subscriber.
RESERVE_IPS = (
    "160.119.126.160",
    "160.119.126.161",
    "160.119.126.162",
    "160.119.126.163",
)


def _fail(msg: str) -> None:
    print(f"ABORT: {msg}")
    raise SystemExit(2)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--apply", action="store_true", help="Write changes. Default: dry-run."
    )
    parser.add_argument(
        "--no-coa", action="store_true", help="Skip the CoA session kick."
    )
    args = parser.parse_args()

    db = SessionLocal()
    try:
        subscriber = db.get(Subscriber, coerce_uuid(SUBSCRIBER_ID))
        if subscriber is None:
            _fail(f"subscriber {SUBSCRIBER_ID} not found")

        sub = (
            db.query(Subscription)
            .filter(Subscription.subscriber_id == subscriber.id)
            .filter(Subscription.login == EXPECTED_LOGIN)
            .first()
        )
        if sub is None:
            _fail(f"no subscription with login {EXPECTED_LOGIN} for this subscriber")

        route = (
            db.query(SubscriberAdditionalRoute)
            .filter(SubscriberAdditionalRoute.subscriber_id == subscriber.id)
            .filter(SubscriberAdditionalRoute.cidr == ROUTE_CIDR)
            .first()
        )
        if route is None:
            _fail(f"SubscriberAdditionalRoute {ROUTE_CIDR} not found for subscriber")
        if route.is_active:
            print("NOTHING TO DO: route is already active.")
            return 0

        # No OTHER active route may overlap the /30 we're about to re-enable.
        target_net = ipaddress.ip_network(ROUTE_CIDR, strict=False)
        for other in db.query(SubscriberAdditionalRoute).filter(
            SubscriberAdditionalRoute.is_active.is_(True)
        ):
            try:
                other_net = ipaddress.ip_network(str(other.cidr), strict=False)
            except ValueError:
                continue
            if other_net.overlaps(target_net):
                _fail(
                    f"active route {other.cidr} (subscriber {other.subscriber_id}) "
                    f"overlaps {ROUTE_CIDR} — manual review required"
                )

        # The stale duplicate assignments must belong to THIS subscriber and be
        # inactive — never reactivate them as primaries.
        dup_assignments = []
        for ip in STALE_DUP_IPS:
            addr = db.query(IPv4Address).filter(IPv4Address.address == ip).first()
            if addr is None:
                continue  # already cleaned up
            for a in db.query(IPAssignment).filter(
                IPAssignment.ipv4_address_id == addr.id
            ):
                if a.subscriber_id != subscriber.id:
                    _fail(
                        f"{ip} assignment belongs to {a.subscriber_id}, not this subscriber"
                    )
                if a.is_active:
                    _fail(
                        f"{ip} has an ACTIVE assignment — not a stale duplicate, aborting"
                    )
                dup_assignments.append(a)

        reserve_rows = [
            db.query(IPv4Address).filter(IPv4Address.address == ip).first()
            for ip in RESERVE_IPS
        ]

        print("=== c8739bc8 routed-/30 repair plan ===")
        print(f"  reactivate route        : {ROUTE_CIDR}")
        print(f"  delete dup assignments  : {[str(a.id) for a in dup_assignments]}")
        print(
            "  reserve /30 member rows : "
            f"{[r.address for r in reserve_rows if r is not None]}"
        )
        print(f"  refresh RADIUS + CoA    : login {EXPECTED_LOGIN} (sub {sub.id})")

        if not args.apply:
            print("\nDRY RUN — no changes written. Re-run with --apply.")
            return 0

        route.is_active = True
        for a in dup_assignments:
            db.delete(a)
        for r in reserve_rows:
            if r is not None:
                r.is_reserved = True
        db.commit()
        print("applied DB changes.")

        try:
            from app.services.radius_population import populate
        except ImportError:
            from scripts.migration.populate_radius_from_subs import populate

        populate(dry_run=False)
        print("RADIUS refreshed.")

        if not args.no_coa:
            from app.services.enforcement import disconnect_subscription_sessions

            kicked = disconnect_subscription_sessions(
                db, str(sub.id), reason="routed-/30 restore (c8739bc8)"
            )
            print(f"CoA kicked {kicked} session(s).")
        return 0
    finally:
        db.close()


if __name__ == "__main__":
    sys.exit(main())
