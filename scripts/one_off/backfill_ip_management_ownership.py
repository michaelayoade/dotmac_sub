#!/usr/bin/env python3
"""Backfill IPv4 IPAM ownership from customer and ONT device records.

This script reconciles three IPv4 ownership sources into IPAM:
1. Subscription/customer records with ``subscriptions.ipv4_address``
2. Active ONT WAN static IPs from ``ont_assignments.static_ip``
3. Active ONT management IPs from ``ont_assignments.mgmt_ip_address`` /
   ``ont_units.mgmt_ip_address``

It is intentionally conservative by default:
- missing IPAM rows are created
- existing rows are updated to match current ownership
- stale IPAM rows are left alone unless ``--deactivate-stale`` is passed

Usage:
    poetry run python scripts/one_off/backfill_ip_management_ownership.py
    poetry run python scripts/one_off/backfill_ip_management_ownership.py --execute
    poetry run python scripts/one_off/backfill_ip_management_ownership.py --execute --deactivate-stale
"""

from __future__ import annotations

import argparse
import ipaddress
import logging
from dataclasses import dataclass

from sqlalchemy.orm import joinedload

from app.db import SessionLocal
from app.models.catalog import Subscription
from app.models.network import IPAssignment, IPv4Address, IPVersion, OntAssignment
from app.services import network as network_service
from app.services import web_network_ip

logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")
logger = logging.getLogger(__name__)

MANAGEMENT_ALLOCATION_TYPE = "management"
WAN_ALLOCATION_TYPE = "wan"


@dataclass
class ExpectedAssignment:
    ip_address: str
    subscriber_id: object
    service_address_id: object
    source: str
    reference_id: str


@dataclass
class ExpectedManagement:
    ip_address: str
    ont_id: object
    source: str


def _normalize_ipv4(value: object) -> str | None:
    raw = str(value or "").strip()
    if not raw:
        return None
    try:
        parsed = ipaddress.ip_address(raw)
    except ValueError:
        return None
    if not isinstance(parsed, ipaddress.IPv4Address):
        return None
    return str(parsed)


def _collect_expected_assignments(
    db,
) -> tuple[dict[str, ExpectedAssignment], dict[str, int]]:
    expected: dict[str, ExpectedAssignment] = {}
    stats = {
        "subscriptions_seen": 0,
        "subscription_conflicts": 0,
        "subscription_invalid_ipv4": 0,
        "subscription_missing_subscriber": 0,
        "ont_wan_seen": 0,
        "ont_wan_conflicts": 0,
        "ont_wan_invalid_ipv4": 0,
        "ont_wan_missing_subscriber": 0,
    }

    subscriptions = (
        db.query(Subscription)
        .filter(Subscription.ipv4_address.isnot(None))
        .filter(Subscription.ipv4_address != "")
        .order_by(Subscription.updated_at.desc())
        .all()
    )
    for subscription in subscriptions:
        stats["subscriptions_seen"] += 1
        ip_address = _normalize_ipv4(subscription.ipv4_address)
        if ip_address is None:
            stats["subscription_invalid_ipv4"] += 1
            continue
        if getattr(subscription, "subscriber_id", None) is None:
            stats["subscription_missing_subscriber"] += 1
            continue
        candidate = ExpectedAssignment(
            ip_address=ip_address,
            subscriber_id=subscription.subscriber_id,
            service_address_id=getattr(subscription, "service_address_id", None),
            source="subscription",
            reference_id=str(subscription.id),
        )
        existing = expected.get(ip_address)
        if existing is None:
            expected[ip_address] = candidate
            continue
        if str(existing.subscriber_id) == str(candidate.subscriber_id) and str(
            existing.service_address_id
        ) == str(candidate.service_address_id):
            continue
        stats["subscription_conflicts"] += 1

    ont_assignments = (
        db.query(OntAssignment)
        .options(joinedload(OntAssignment.ont_unit))
        .filter(OntAssignment.active.is_(True))
        .filter(OntAssignment.static_ip.isnot(None))
        .filter(OntAssignment.static_ip != "")
        .order_by(OntAssignment.updated_at.desc())
        .all()
    )
    for assignment in ont_assignments:
        stats["ont_wan_seen"] += 1
        ip_address = _normalize_ipv4(assignment.static_ip)
        if ip_address is None:
            stats["ont_wan_invalid_ipv4"] += 1
            continue
        if getattr(assignment, "subscriber_id", None) is None:
            stats["ont_wan_missing_subscriber"] += 1
            continue
        candidate = ExpectedAssignment(
            ip_address=ip_address,
            subscriber_id=assignment.subscriber_id,
            service_address_id=getattr(assignment, "service_address_id", None),
            source="ont_static_wan",
            reference_id=str(assignment.ont_unit_id),
        )
        existing = expected.get(ip_address)
        if existing is None:
            expected[ip_address] = candidate
            continue
        if str(existing.subscriber_id) == str(candidate.subscriber_id) and str(
            existing.service_address_id
        ) == str(candidate.service_address_id):
            continue
        stats["ont_wan_conflicts"] += 1

    return expected, stats


def _collect_expected_management(
    db,
) -> tuple[dict[str, ExpectedManagement], dict[str, int]]:
    expected: dict[str, ExpectedManagement] = {}
    stats = {
        "ont_mgmt_seen": 0,
        "ont_mgmt_conflicts": 0,
        "ont_mgmt_invalid_ipv4": 0,
    }
    ont_assignments = (
        db.query(OntAssignment)
        .options(joinedload(OntAssignment.ont_unit))
        .filter(OntAssignment.active.is_(True))
        .all()
    )
    for assignment in ont_assignments:
        ont = getattr(assignment, "ont_unit", None)
        if ont is None:
            continue
        mgmt_ip = assignment.mgmt_ip_address or getattr(ont, "mgmt_ip_address", None)
        ip_address = _normalize_ipv4(mgmt_ip)
        if not mgmt_ip:
            continue
        stats["ont_mgmt_seen"] += 1
        if ip_address is None:
            stats["ont_mgmt_invalid_ipv4"] += 1
            continue
        candidate = ExpectedManagement(
            ip_address=ip_address,
            ont_id=ont.id,
            source=f"ont:{ont.id}",
        )
        existing = expected.get(ip_address)
        if existing is None:
            expected[ip_address] = candidate
            continue
        if str(existing.ont_id) == str(candidate.ont_id):
            continue
        stats["ont_mgmt_conflicts"] += 1
    return expected, stats


def _address_rows_by_ip(db) -> dict[str, IPv4Address]:
    rows = db.query(IPv4Address).options(joinedload(IPv4Address.assignment)).all()
    return {
        str(row.address).strip(): row
        for row in rows
        if str(getattr(row, "address", "") or "").strip()
    }


def _sync_assignment(
    db,
    *,
    row: IPv4Address,
    expected: ExpectedAssignment,
    stats: dict[str, int],
) -> None:
    assignment = getattr(row, "assignment", None)
    if assignment is None:
        assignment = IPAssignment(
            subscriber_id=expected.subscriber_id,
            service_address_id=expected.service_address_id,
            ip_version=IPVersion.ipv4,
            ipv4_address_id=row.id,
            is_active=True,
        )
        db.add(assignment)
        row.assignment = assignment
        stats["assignments_created"] += 1
    else:
        changed = False
        if str(getattr(assignment, "subscriber_id", "") or "") != str(
            expected.subscriber_id
        ):
            assignment.subscriber_id = expected.subscriber_id
            changed = True
        if str(getattr(assignment, "service_address_id", "") or "") != str(
            expected.service_address_id
        ):
            assignment.service_address_id = expected.service_address_id
            changed = True
        if getattr(assignment, "ip_version", None) != IPVersion.ipv4:
            assignment.ip_version = IPVersion.ipv4
            changed = True
        if str(getattr(assignment, "ipv4_address_id", "") or "") != str(row.id):
            assignment.ipv4_address_id = row.id
            changed = True
        if getattr(assignment, "ipv6_address_id", None) is not None:
            assignment.ipv6_address_id = None
            changed = True
        if not getattr(assignment, "is_active", False):
            assignment.is_active = True
            changed = True
            stats["assignments_reactivated"] += 1
        if changed:
            stats["assignments_updated"] += 1
    if (
        str(getattr(row, "allocation_type", "") or "").strip()
        != MANAGEMENT_ALLOCATION_TYPE
    ):
        row.allocation_type = WAN_ALLOCATION_TYPE


def _sync_management(
    row: IPv4Address,
    *,
    expected: ExpectedManagement,
    stats: dict[str, int],
) -> None:
    changed = False
    if not getattr(row, "is_reserved", False):
        row.is_reserved = True
        changed = True
    expected_note = f"ont:{expected.ont_id}"
    if str(getattr(row, "notes", "") or "").strip() != expected_note:
        row.notes = expected_note
        changed = True
    if str(getattr(row, "ont_unit_id", "") or "") != str(expected.ont_id):
        row.ont_unit_id = expected.ont_id
        changed = True
    if (
        str(getattr(row, "allocation_type", "") or "").strip()
        != MANAGEMENT_ALLOCATION_TYPE
    ):
        row.allocation_type = MANAGEMENT_ALLOCATION_TYPE
        changed = True
    if changed:
        stats["management_rows_updated"] += 1


def _clear_stale_management_rows(
    rows_by_ip: dict[str, IPv4Address],
    *,
    expected_management_ips: set[str],
    stats: dict[str, int],
) -> None:
    for ip_address, row in rows_by_ip.items():
        allocation_type = str(getattr(row, "allocation_type", "") or "").strip()
        if (
            allocation_type != MANAGEMENT_ALLOCATION_TYPE
            and getattr(row, "ont_unit_id", None) is None
        ):
            continue
        if ip_address in expected_management_ips:
            continue
        assignment = getattr(row, "assignment", None)
        if assignment is not None and getattr(assignment, "is_active", False):
            continue
        row.is_reserved = False
        row.notes = None
        row.ont_unit_id = None
        row.allocation_type = None
        stats["management_rows_cleared"] += 1


def _deactivate_stale_assignments(
    db,
    *,
    expected_assignment_ips: set[str],
    stats: dict[str, int],
) -> None:
    active_assignments = (
        db.query(IPAssignment)
        .options(joinedload(IPAssignment.ipv4_address))
        .filter(IPAssignment.ip_version == IPVersion.ipv4)
        .filter(IPAssignment.is_active.is_(True))
        .all()
    )
    for assignment in active_assignments:
        row = getattr(assignment, "ipv4_address", None)
        ip_address = str(getattr(row, "address", "") or "").strip()
        if ip_address and ip_address in expected_assignment_ips:
            continue
        assignment.is_active = False
        stats["assignments_deactivated"] += 1


def _reconcile_pool_memberships(db, *, commit: bool) -> dict[str, int]:
    pools = [
        pool
        for pool in network_service.ip_pools.list(
            db=db,
            ip_version="ipv4",
            is_active=None,
            order_by="name",
            order_dir="asc",
            limit=5000,
            offset=0,
        )
        if web_network_ip._parse_network(str(pool.cidr)) is not None
    ]
    pool_networks = [
        (pool, web_network_ip._parse_network(str(pool.cidr))) for pool in pools
    ]

    updated = 0
    unchanged = 0
    unmatched = 0
    invalid = 0
    conflicts = 0
    for row in db.query(IPv4Address).all():
        try:
            address = ipaddress.ip_address(str(row.address))
        except ValueError:
            invalid += 1
            continue
        matches = [
            pool
            for pool, network in pool_networks
            if network is not None and address in network
        ]
        if not matches:
            unmatched += 1
            continue
        if len(matches) > 1:
            conflicts += 1
            continue
        target_pool = matches[0]
        if str(getattr(row, "pool_id", "") or "") == str(target_pool.id):
            unchanged += 1
            continue
        row.pool_id = target_pool.id
        updated += 1
    if commit and updated:
        db.commit()
    return {
        "updated": updated,
        "unchanged": unchanged,
        "unmatched": unmatched,
        "invalid": invalid,
        "conflicts": conflicts,
    }


def run_backfill(*, execute: bool, deactivate_stale: bool) -> dict[str, int]:
    db = SessionLocal()
    try:
        stats: dict[str, int] = {
            "addresses_created": 0,
            "assignments_created": 0,
            "assignments_updated": 0,
            "assignments_reactivated": 0,
            "assignments_deactivated": 0,
            "assignment_conflicts_with_mgmt": 0,
            "management_rows_updated": 0,
            "management_rows_cleared": 0,
            "management_conflicts_with_assignment": 0,
        }
        expected_assignments, assignment_stats = _collect_expected_assignments(db)
        expected_management, management_stats = _collect_expected_management(db)
        stats.update(assignment_stats)
        stats.update(management_stats)

        rows_by_ip = _address_rows_by_ip(db)

        for ip_address, expected in expected_assignments.items():
            row = rows_by_ip.get(ip_address)
            if row is None:
                row = IPv4Address(
                    address=ip_address, allocation_type=WAN_ALLOCATION_TYPE
                )
                db.add(row)
                db.flush()
                rows_by_ip[ip_address] = row
                stats["addresses_created"] += 1
            if (
                str(getattr(row, "allocation_type", "") or "").strip()
                == MANAGEMENT_ALLOCATION_TYPE
            ):
                if str(getattr(row, "ont_unit_id", "") or "") != "":
                    stats["assignment_conflicts_with_mgmt"] += 1
                    continue
            _sync_assignment(db, row=row, expected=expected, stats=stats)

        for ip_address, expected in expected_management.items():
            row = rows_by_ip.get(ip_address)
            if row is None:
                row = IPv4Address(
                    address=ip_address,
                    is_reserved=True,
                    notes=f"ont:{expected.ont_id}",
                    ont_unit_id=expected.ont_id,
                    allocation_type=MANAGEMENT_ALLOCATION_TYPE,
                )
                db.add(row)
                db.flush()
                rows_by_ip[ip_address] = row
                stats["addresses_created"] += 1
                stats["management_rows_updated"] += 1
                continue
            assignment = getattr(row, "assignment", None)
            if assignment is not None and getattr(assignment, "is_active", False):
                stats["management_conflicts_with_assignment"] += 1
                continue
            _sync_management(row, expected=expected, stats=stats)

        if deactivate_stale:
            _deactivate_stale_assignments(
                db,
                expected_assignment_ips=set(expected_assignments),
                stats=stats,
            )
            _clear_stale_management_rows(
                rows_by_ip,
                expected_management_ips=set(expected_management),
                stats=stats,
            )

        reconcile_stats = _reconcile_pool_memberships(db, commit=False)
        stats["pool_memberships_updated"] = int(reconcile_stats.get("updated", 0))
        stats["pool_memberships_unchanged"] = int(reconcile_stats.get("unchanged", 0))
        stats["pool_memberships_unmatched"] = int(reconcile_stats.get("unmatched", 0))
        stats["pool_memberships_invalid"] = int(reconcile_stats.get("invalid", 0))
        stats["pool_memberships_conflicts"] = int(reconcile_stats.get("conflicts", 0))

        if execute:
            db.commit()
        else:
            db.rollback()
        return stats
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Backfill IPv4 IPAM ownership from customer and device records."
    )
    parser.add_argument(
        "--execute",
        action="store_true",
        help="Apply changes. Default is dry-run.",
    )
    parser.add_argument(
        "--deactivate-stale",
        action="store_true",
        help="Also deactivate/clear stale IPAM ownership not backed by current records.",
    )
    args = parser.parse_args()

    logger.info("=" * 60)
    logger.info("Mode: %s", "EXECUTE" if args.execute else "DRY RUN")
    logger.info("Deactivate stale: %s", "yes" if args.deactivate_stale else "no")
    logger.info("=" * 60)

    stats = run_backfill(
        execute=args.execute,
        deactivate_stale=args.deactivate_stale,
    )
    for key in sorted(stats):
        logger.info("%s=%s", key, stats[key])


if __name__ == "__main__":
    main()
