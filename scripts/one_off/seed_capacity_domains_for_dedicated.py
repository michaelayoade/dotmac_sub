"""Name the PON ports that carry dedicated circuits, so they can be surveyed.

The dedicated CIR cutover cannot proceed until the segments those circuits sit
on have a recorded capacity. Surveying all 502 PON ports to get there would be
absurd; only the ports actually carrying a dedicated subscription gate the
cutover, and there are far fewer.

This creates a ``CapacityDomain`` for each of those ports with **no capacity
figure** — deliberately. The row makes the port enumerable as survey backlog
and gives the measurement somewhere to land. Until it is measured the resolver
reports ``unknown``, which refuses new sales on that segment rather than waving
them through.

``target_oversubscription`` is left at 1 (1:1). A dedicated circuit's committed
rate is reserved whether or not it is used, so overselling against it is not
statistical multiplexing, it is a promise that cannot be kept.

Usage:
    python -m scripts.one_off.seed_capacity_domains_for_dedicated --dry-run
    python -m scripts.one_off.seed_capacity_domains_for_dedicated --live
"""

from __future__ import annotations

import argparse

from app.db import SessionLocal
from app.models.capacity import CapacityDomain, CapacityDomainKind
from app.models.catalog import CatalogOffer, Subscription, SubscriptionStatus
from app.models.network import OLTDevice, OntAssignment, PonPort


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--dry-run", action="store_true")
    mode.add_argument("--live", action="store_true")
    parser.add_argument(
        "--family",
        default="dedicated",
        help="plan family whose ports to enumerate (default: dedicated)",
    )
    args = parser.parse_args()

    db = SessionLocal()
    created = 0
    existing = 0
    try:
        rows = (
            db.query(
                PonPort.id,
                PonPort.name,
                OLTDevice.name,
                OLTDevice.vendor,
                CatalogOffer.speed_download_mbps,
            )
            .select_from(OntAssignment)
            .join(PonPort, PonPort.id == OntAssignment.pon_port_id)
            .join(OLTDevice, OLTDevice.id == PonPort.olt_id)
            .join(
                Subscription,
                Subscription.subscriber_id == OntAssignment.subscriber_id,
            )
            .join(CatalogOffer, CatalogOffer.id == Subscription.offer_id)
            .filter(
                OntAssignment.active.is_(True),
                OntAssignment.pon_port_id.isnot(None),
                Subscription.status == SubscriptionStatus.active,
                CatalogOffer.plan_family == args.family,
            )
            .all()
        )

        committed: dict[object, int] = {}
        labels: dict[object, str] = {}
        for pon_id, pon_name, olt_name, vendor, speed in rows:
            committed[pon_id] = committed.get(pon_id, 0) + int(speed or 0)
            labels[pon_id] = f"{olt_name} [{vendor or '?'}] {pon_name}"

        already = {
            domain.pon_port_id
            for domain in db.query(CapacityDomain)
            .filter(CapacityDomain.kind == CapacityDomainKind.pon_port)
            .all()
        }

        for pon_id, label in sorted(labels.items(), key=lambda kv: -committed[kv[0]]):
            if pon_id in already:
                existing += 1
                continue
            print(f"  + {label:52} committed {committed[pon_id]:>7} Mbps")
            db.add(
                CapacityDomain(
                    kind=CapacityDomainKind.pon_port,
                    name=label,
                    pon_port_id=pon_id,
                    downstream_mbps=None,
                    upstream_mbps=None,
                    target_oversubscription=1,
                    notes=(
                        f"Carries {args.family} circuits committing "
                        f"{committed[pon_id]} Mbps. Capacity NOT surveyed — "
                        "record downstream/upstream and capacity_source before "
                        "relying on any verdict for this segment."
                    ),
                )
            )
            created += 1

        if args.live:
            db.commit()
            print(f"\napplied: {created} domain(s) created, {existing} already present")
        else:
            db.rollback()
            print(
                f"\ndry-run: {created} domain(s) would be created, "
                f"{existing} already present"
            )
        print("Every new domain has NO capacity figure and reports 'unknown'.")
    finally:
        db.close()


if __name__ == "__main__":
    main()
