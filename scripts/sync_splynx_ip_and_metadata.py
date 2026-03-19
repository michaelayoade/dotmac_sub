#!/usr/bin/env python3
"""Sync IP pools, IPv6 networks, VLAN names, pool types, and NAS linkage from Splynx.

Fixes:
1. IP pool types (static/management/pool) from Splynx type_of_usage
2. IP pool names from Splynx descriptive titles
3. IPv6 pools (23 /48 ranges from Splynx, currently 0 in DotMac Sub)
4. VLAN descriptive names from Splynx network titles
5. NAS → monitoring device linkage (network_device_id FK)
6. Allen OLT vendor/model metadata

Usage:
    PYTHONPATH=. poetry run python scripts/sync_splynx_ip_and_metadata.py [--dry-run]
"""

from __future__ import annotations

import argparse
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
    if result.returncode != 0:
        logger.error("SSH/MySQL failed: %s", result.stderr.strip()[:200])
        return ""
    return result.stdout.strip()


def fix_ip_pool_metadata(db, dry_run: bool) -> dict[str, int]:
    """Update IP pool names and types from Splynx data."""
    from app.services.network.ip import IpPools

    # Fetch Splynx networks
    raw = _ssh_query(
        "SELECT n.network, n.mask, n.title, n.type_of_usage "
        "FROM ipv4_networks n WHERE n.deleted = '0' ORDER BY INET_ATON(n.network)"
    )
    splynx_nets: dict[str, dict[str, int | str]] = {}
    for line in raw.split("\n"):
        if not line.strip():
            continue
        parts = line.split("\t")
        if len(parts) < 4:
            continue
        network = parts[0].strip()
        splynx_nets[network] = {
            "mask": int(parts[1]),
            "title": parts[2].strip(),
            "usage": parts[3].strip(),
        }

    logger.info("Loaded %d IPv4 networks from Splynx", len(splynx_nets))

    # Map Splynx type_of_usage → DotMac pool_type
    usage_map = {
        "pool": "dynamic",
        "static": "static",
        "management": "management",
    }

    # Update existing pools
    pools = IpPools.list(db, ip_version="ipv4", is_active=True, order_by="name", order_dir="asc", limit=200, offset=0)
    updated = 0
    for pool in pools:
        # Extract network from pool name (e.g. "Pool-100-172.16.98.0" → "172.16.98.0")
        pool_name = pool.name or ""
        network_hint = pool_name.split("-")[-1] if "-" in pool_name else ""

        match = splynx_nets.get(network_hint)
        if not match:
            continue

        changed = False
        new_name = match["title"]
        usage = match.get("usage")
        new_type = usage_map.get(usage) if isinstance(usage, str) else None

        if new_name and pool.name != new_name:
            # Append subnet hint if name would collide (e.g. two "Point To Point IPs")
            unique_name = new_name
            from sqlalchemy import select as sa_select

            from app.models.network import IpPool
            existing_with_name = db.scalars(
                sa_select(IpPool).where(IpPool.name == new_name, IpPool.id != pool.id)
            ).first()
            if existing_with_name:
                unique_name = f"{new_name} ({network_hint})"
            if not dry_run:
                pool.name = unique_name
            changed = True

        # pool_type is tracked in notes since model doesn't have a type field
        if new_type and pool.notes != f"type: {new_type}":
            if not dry_run:
                pool.notes = f"type: {new_type}"
            changed = True

        if changed:
            if not dry_run:
                try:
                    db.flush()
                except Exception as e:
                    db.rollback()
                    logger.warning("  Skipped %s (name conflict): %s", pool_name, str(e)[:80])
                    continue
            updated += 1
            logger.info("  %s Pool %s → name='%s' type=%s", "[DRY]" if dry_run else "Updated", pool_name, unique_name, new_type)

    if not dry_run:
        db.commit()
    return {"updated": updated}


def import_ipv6_pools(db, dry_run: bool) -> dict[str, int]:
    """Import IPv6 /48 ranges from Splynx."""
    raw = _ssh_query(
        "SELECT network, prefix, title, type_of_usage FROM ipv6_networks ORDER BY id"
    )
    if not raw:
        return {"created": 0}

    from app.models.network import IpPool

    existing = {p.name: p for p in db.query(IpPool).filter(IpPool.ip_version == "ipv6").all()}
    created = 0

    for line in raw.split("\n"):
        if not line.strip():
            continue
        parts = line.split("\t")
        if len(parts) < 4:
            continue
        network = parts[0].strip()
        prefix = int(parts[1])
        title = parts[2].strip()

        if title in existing:
            continue

        if dry_run:
            logger.info("  [DRY] Would create IPv6 pool: %s (%s/%d)", title, network, prefix)
        else:
            from app.models.network import IPVersion
            pool = IpPool(
                name=title,
                ip_version=IPVersion.ipv6,
                cidr=f"{network}/{prefix}",
                is_active=True,
                notes=f"Imported from Splynx (prefix /{prefix})",
            )
            db.add(pool)
            logger.info("  Created IPv6 pool: %s (%s/%d)", title, network, prefix)
        created += 1

    if not dry_run:
        db.commit()
    return {"created": created}


def fix_vlan_names(db, dry_run: bool) -> dict[str, int]:
    """Update VLAN names from Splynx network titles based on tag matching."""
    from app.models.network import Vlan

    # Map VLAN tags to descriptive names from the Splynx data + OLT config knowledge
    vlan_names = {
        30: "Test Management",
        153: "Direct Broadband",
        201: "OLT Management",
        202: "OLT Management 2",
        203: "PPPoE Service (Garki/Gudu)",
        205: "PPPoE Service (Gwarimpa)",
        207: "PPPoE Service (BOI/Asokoro)",
        208: "PPPoE Service (Jabi)",
        209: "PPPoE Service (Reserved)",
        210: "PPPoE Service (Karsana)",
        211: "PPPoE Service (SPDC)",
        212: "PPPoE Service (Kubwa)",
        213: "PPPoE Service (Lokogoma)",
        298: "PPPoE Service (IDU)",
        299: "PPPoE Service (Eagle FM)",
        300: "PPPoE Service (Airport)",
        400: "PPPoE Service (Maitama/AFR)",
    }

    vlans = db.query(Vlan).filter(Vlan.is_active.is_(True)).all()
    updated = 0
    for v in vlans:
        new_name = vlan_names.get(v.tag)
        if new_name and v.name != new_name:
            old = v.name
            if not dry_run:
                v.name = new_name
            updated += 1
            logger.info("  %s VLAN %d: '%s' → '%s'", "[DRY]" if dry_run else "Updated", v.tag, old, new_name)

    if not dry_run:
        db.commit()
    return {"updated": updated}


def fix_nas_monitoring_linkage(db, dry_run: bool) -> dict[str, int]:
    """Link NAS devices to their monitoring NetworkDevice counterparts by IP."""
    from app.models.catalog import NasDevice
    from app.models.network_monitoring import NetworkDevice

    nas_devices = db.query(NasDevice).filter(
        NasDevice.is_active.is_(True),
        NasDevice.network_device_id.is_(None),
    ).all()

    # Build IP → NetworkDevice lookup
    mon_by_ip = {}
    for d in db.query(NetworkDevice).filter(NetworkDevice.is_active.is_(True)).all():
        if d.mgmt_ip:
            mon_by_ip[d.mgmt_ip] = d

    linked = 0
    for nas in nas_devices:
        ip = nas.management_ip or nas.ip_address
        if not ip:
            continue
        mon = mon_by_ip.get(ip)
        if mon:
            if not dry_run:
                nas.network_device_id = mon.id
            linked += 1
            logger.info("  %s NAS '%s' (%s) → monitoring device %s", "[DRY]" if dry_run else "Linked", nas.name, ip, mon.id)

    if not dry_run:
        db.commit()
    return {"linked": linked, "unlinked": len(nas_devices) - linked}


def fix_allen_olt(db, dry_run: bool) -> dict[str, int]:
    """Fill in missing Allen OLT vendor/model metadata."""
    from app.models.network import OLTDevice

    allen = db.query(OLTDevice).filter(OLTDevice.name.ilike("%Allen%")).first()
    if not allen:
        return {"updated": 0}
    if allen.vendor and allen.model:
        return {"updated": 0}

    if not dry_run:
        allen.vendor = "Huawei"
        allen.model = "MA5608T"
        db.commit()
    logger.info("  %s Allen OLT: vendor=Huawei model=MA5608T", "[DRY]" if dry_run else "Updated")
    return {"updated": 1}


def main():
    parser = argparse.ArgumentParser(description="Sync IP metadata from Splynx")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    from app.db import SessionLocal
    db = SessionLocal()

    logger.info("=== IP Pool Metadata ===")
    r1 = fix_ip_pool_metadata(db, args.dry_run)
    logger.info("Result: %s", r1)

    logger.info("\n=== IPv6 Pools ===")
    r2 = import_ipv6_pools(db, args.dry_run)
    logger.info("Result: %s", r2)

    logger.info("\n=== VLAN Names ===")
    r3 = fix_vlan_names(db, args.dry_run)
    logger.info("Result: %s", r3)

    logger.info("\n=== NAS → Monitoring Linkage ===")
    r4 = fix_nas_monitoring_linkage(db, args.dry_run)
    logger.info("Result: %s", r4)

    logger.info("\n=== Allen OLT Metadata ===")
    r5 = fix_allen_olt(db, args.dry_run)
    logger.info("Result: %s", r5)

    logger.info("\n=== DONE ===")
    db.close()


if __name__ == "__main__":
    main()
