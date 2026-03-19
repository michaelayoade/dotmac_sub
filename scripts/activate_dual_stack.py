#!/usr/bin/env python3
"""Activate dual-stack IPv4+IPv6 for the ISP platform.

Steps:
1. Generate IPv6 /64 delegation prefixes in each /48 pool
2. Set ipv6_pool_name on RADIUS profiles (matching POP geography)
3. Import IPv4 address assignments from Splynx
4. Assign IPv6 prefixes to active subscribers
5. Summary report

Usage:
    PYTHONPATH=. REDIS_URL=redis://:xxx@localhost:6379/0 \
    poetry run python scripts/activate_dual_stack.py [--dry-run]
"""

from __future__ import annotations

import argparse
import ipaddress
import logging
import subprocess

logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")
logger = logging.getLogger(__name__)


def _ssh_query(query: str) -> str:
    cmd = [
        "ssh", "-o", "ConnectTimeout=10", "-o", "StrictHostKeyChecking=no",
        "root@138.68.165.175",
        f'mysql -u root -N -B -e "{query}" splynx',
    ]
    result = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
    return result.stdout.strip() if result.returncode == 0 else ""


def generate_ipv6_prefixes(db, dry_run: bool) -> dict[str, int]:
    """Generate /64 delegation prefixes inside each /48 IPv6 pool.

    ISP pattern: /48 per POP → /64 per subscriber.
    Each /48 contains 65536 possible /64 prefixes.
    We pre-generate the first 1000 per pool for allocation.
    """
    from app.models.network import IPVersion, IpPool, IPv6Address

    pools = db.query(IpPool).filter(
        IpPool.ip_version == IPVersion.ipv6,
        IpPool.is_active.is_(True),
    ).all()

    total_created = 0
    for pool in pools:
        existing = db.query(IPv6Address).filter(IPv6Address.pool_id == pool.id).count()
        if existing > 0:
            logger.info("  Pool '%s' already has %d prefixes, skipping", pool.name, existing)
            continue

        try:
            network = ipaddress.IPv6Network(pool.cidr, strict=False)
        except ValueError as e:
            logger.warning("  Invalid CIDR for pool '%s': %s", pool.name, e)
            continue

        # For /48 pools, generate /64 delegation prefixes
        # For /64 pools, generate /128 host addresses
        if network.prefixlen <= 48:
            target_prefix = 64
        elif network.prefixlen <= 64:
            target_prefix = 128
        else:
            logger.info("  Pool '%s' prefix /%d too narrow for delegation", pool.name, network.prefixlen)
            continue

        count = 0
        max_per_pool = 1000  # Pre-generate first 1000
        for subnet in network.subnets(new_prefix=target_prefix):
            if count >= max_per_pool:
                break
            if count == 0:
                # Skip network address (::0)
                count += 1
                continue

            prefix_str = f"{subnet.network_address}/{target_prefix}"
            if dry_run:
                if count <= 3:
                    logger.info("  [DRY] %s → %s", pool.name, prefix_str)
            else:
                addr = IPv6Address(
                    pool_id=pool.id,
                    address=prefix_str,
                )
                db.add(addr)

            count += 1
            total_created += 1

        if not dry_run and count > 1:
            db.flush()
        logger.info("  %s '%s': %d prefixes (/%d)", "[DRY]" if dry_run else "Generated", pool.name, count - 1, target_prefix)

    if not dry_run:
        db.commit()
    return {"created": total_created}


def set_ipv6_on_radius_profiles(db, dry_run: bool) -> dict[str, int]:
    """Set ipv6_pool_name on RADIUS profiles based on POP-to-pool mapping.

    FreeRADIUS uses Delegated-IPv6-Prefix-Pool to assign /64 prefixes.
    The pool name must match a FreeRADIUS ipv6pool definition.
    """
    from app.models.catalog import RadiusProfile

    profiles = db.query(RadiusProfile).filter(RadiusProfile.is_active.is_(True)).all()
    updated = 0

    # For now, set a generic pool name that the RADIUS config can reference.
    # The actual pool-to-POP mapping happens in FreeRADIUS ippool config.
    pool_name = "ipv6_delegation"

    for profile in profiles:
        if profile.ipv6_pool_name:
            continue  # Already set

        if not dry_run:
            profile.ipv6_pool_name = pool_name
        updated += 1

    if not dry_run:
        db.commit()
    logger.info("  %s %d RADIUS profiles with ipv6_pool_name='%s'",
                "[DRY]" if dry_run else "Updated", updated, pool_name)
    return {"updated": updated}


def import_ipv4_from_splynx(db, dry_run: bool) -> dict[str, int]:
    """Import IPv4 address assignments from Splynx into ip pools.

    Reads assigned IPs from Splynx ipv4_networks_ip table and creates
    IPv4Address records in the matching pool.
    """
    from app.models.network import IPVersion, IpPool, IPv4Address

    # Fetch assigned IPs from Splynx
    raw = _ssh_query(
        "SELECT ip.id, INET_NTOA(ip.ip) as addr, n.network, n.mask, n.title, "
        "ip.customer_id "
        "FROM ipv4_networks_ip ip "
        "JOIN ipv4_networks n ON ip.ipv4_networks_id = n.id "
        "WHERE n.deleted = '0' AND ip.customer_id > 0 "
        "ORDER BY INET_ATON(ip.ip)"
    )
    if not raw:
        logger.warning("  No IPv4 data from Splynx")
        return {"imported": 0}

    # Build pool lookup by CIDR
    pools = db.query(IpPool).filter(IpPool.ip_version == IPVersion.ipv4).all()
    pool_by_network: dict[str, IpPool] = {}
    for p in pools:
        # Extract network from CIDR
        try:
            net = ipaddress.IPv4Network(p.cidr, strict=False)
            pool_by_network[str(net.network_address)] = p
        except ValueError:
            pass

    # Check existing addresses
    existing_addrs = {a.address for a in db.query(IPv4Address.address).all()}

    imported = 0
    skipped = 0
    for line in raw.split("\n"):
        if not line.strip():
            continue
        parts = line.split("\t")
        if len(parts) < 5:
            continue

        addr = parts[1].strip()
        network = parts[2].strip()

        if addr in existing_addrs:
            skipped += 1
            continue

        pool = pool_by_network.get(network)
        if not pool:
            continue

        if dry_run:
            if imported < 5:
                logger.info("  [DRY] %s → pool '%s'", addr, pool.name)
        else:
            ipv4 = IPv4Address(
                pool_id=pool.id,
                address=addr,
            )
            db.add(ipv4)

        imported += 1

    if not dry_run:
        db.commit()
    logger.info("  %s %d IPv4 addresses (%d skipped/existing)",
                "[DRY]" if dry_run else "Imported", imported, skipped)
    return {"imported": imported, "skipped": skipped}


def assign_ipv6_to_subscribers(db, dry_run: bool) -> dict[str, int]:
    """Assign IPv6 /64 prefixes to active subscriptions that have IPv4 but no IPv6."""
    from app.models.catalog import Subscription, SubscriptionStatus
    from app.models.network import (
        IPAssignment,
        IPVersion,
        IpPool,
        IPv6Address,
    )

    # Find active subscriptions with IPv4 but no IPv6
    from sqlalchemy import select

    active_subs = db.scalars(
        select(Subscription).where(
            Subscription.status == SubscriptionStatus.active,
            Subscription.ipv4_address.isnot(None),
            Subscription.ipv6_address.is_(None),
        )
    ).all()

    logger.info("  Found %d active subscriptions with IPv4 but no IPv6", len(active_subs))

    # Get available IPv6 prefixes (not yet assigned)
    assigned_v6_ids = {
        a.ipv6_address_id for a in
        db.query(IPAssignment.ipv6_address_id).filter(
            IPAssignment.ipv6_address_id.isnot(None)
        ).all()
    }

    # Load all IPv6 addresses not yet assigned, grouped by pool
    from collections import defaultdict
    available_by_pool: dict[str, list] = defaultdict(list)
    all_v6 = db.query(IPv6Address).filter(IPv6Address.id.notin_(assigned_v6_ids) if assigned_v6_ids else True).all()
    for addr in all_v6:
        available_by_pool[str(addr.pool_id)].append(addr)

    # For each subscription, pick an IPv6 prefix from any available pool
    # (In production, this should match the subscriber's POP to the right pool)
    all_available = []
    for addrs in available_by_pool.values():
        all_available.extend(addrs)

    assigned = 0
    for sub in active_subs:
        if not all_available:
            logger.warning("  Ran out of IPv6 prefixes after %d assignments", assigned)
            break

        v6_addr = all_available.pop(0)

        if dry_run:
            if assigned < 5:
                logger.info("  [DRY] Sub %s → %s", str(sub.id)[:8], v6_addr.address)
        else:
            sub.ipv6_address = v6_addr.address
            assignment = IPAssignment(
                subscription_id=sub.id,
                subscriber_id=sub.subscriber_id,
                ip_version=IPVersion.ipv6,
                ipv6_address_id=v6_addr.id,
            )
            db.add(assignment)

        assigned += 1

    if not dry_run:
        db.commit()
    logger.info("  %s %d subscribers with IPv6 prefixes", "[DRY]" if dry_run else "Assigned", assigned)
    return {"assigned": assigned, "remaining_prefixes": len(all_available)}


def main():
    parser = argparse.ArgumentParser(description="Activate dual-stack IPv4+IPv6")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    from app.db import SessionLocal
    db = SessionLocal()

    logger.info("=== Step 1: Generate IPv6 /64 delegation prefixes ===")
    r1 = generate_ipv6_prefixes(db, args.dry_run)
    logger.info("Result: %s\n", r1)

    logger.info("=== Step 2: Set IPv6 pool on RADIUS profiles ===")
    r2 = set_ipv6_on_radius_profiles(db, args.dry_run)
    logger.info("Result: %s\n", r2)

    logger.info("=== Step 3: Import IPv4 addresses from Splynx ===")
    r3 = import_ipv4_from_splynx(db, args.dry_run)
    logger.info("Result: %s\n", r3)

    logger.info("=== Step 4: Assign IPv6 to active subscribers ===")
    r4 = assign_ipv6_to_subscribers(db, args.dry_run)
    logger.info("Result: %s\n", r4)

    logger.info("=== COMPLETE ===")
    logger.info("IPv6 prefixes generated: %d", r1["created"])
    logger.info("RADIUS profiles updated: %d", r2["updated"])
    logger.info("IPv4 addresses imported: %d", r3["imported"])
    logger.info("Subscribers assigned IPv6: %d", r4["assigned"])

    db.close()


if __name__ == "__main__":
    main()
