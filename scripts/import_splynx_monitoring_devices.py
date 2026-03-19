#!/usr/bin/env python3
"""Import Splynx monitoring devices into DotMac Sub network_devices table.

Connects to Splynx MySQL via SSH tunnel, reads all active monitoring entries,
and creates NetworkDevice records in the local PostgreSQL database.

Deduplicates by management IP to avoid double-importing devices that
already exist (e.g., the 7 OLTs or NAS-synced devices).

Usage:
    poetry run python scripts/import_splynx_monitoring_devices.py [--dry-run]
"""

from __future__ import annotations

import argparse
import logging
import subprocess
import sys
from datetime import UTC, datetime

logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")
logger = logging.getLogger(__name__)

# Splynx type_id → DotMac DeviceType mapping
TYPE_MAP = {
    "Access Point": "access_point",
    "Router": "router",
    "Switch": "switch",
    "Server": "server",
    "CPE": "modem",
    "Other": "other",
}

# Splynx producer → vendor string
VENDOR_MAP = {
    "Ubiquiti": "ubiquiti",
    "MikroTik": "mikrotik",
    "Huawei": "huawei",
    "Cisco": "cisco",
    "Nokia": "nokia",
    "ZTE": "zte",
    "Other": "other",
}

# Role assignment based on type
ROLE_MAP = {
    "access_point": "access",
    "router": "edge",
    "switch": "distribution",
    "server": "core",
    "modem": "cpe",
    "other": "edge",
}


def fetch_splynx_devices() -> list[dict]:
    """Fetch active monitoring devices from Splynx via SSH + MySQL."""
    query = """
    SELECT m.id, m.title, m.ip, m.model, m.parent_id,
           m.snmp_community, m.snmp_version, m.snmp_port,
           m.is_ping, m.send_notifications, m.delay_timer,
           m.ping_state, m.snmp_state,
           mt.title as type_name,
           mp.title as vendor_name,
           ns.title as site_name
    FROM monitoring m
    LEFT JOIN monitoring_types mt ON m.type = mt.id
    LEFT JOIN monitoring_producers mp ON m.producer = mp.id
    LEFT JOIN network_sites ns ON m.network_site_id = ns.id
    WHERE m.deleted = '0' AND m.active = '1'
    ORDER BY mt.title, m.title
    """
    cmd = [
        "ssh", "-o", "ConnectTimeout=10", "-o", "StrictHostKeyChecking=no",
        "root@138.68.165.175",
        f"mysql -u root -N -B -e \"{query}\" splynx",
    ]
    result = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
    if result.returncode != 0:
        logger.error("SSH/MySQL failed: %s", result.stderr.strip())
        sys.exit(1)

    devices = []
    for line in result.stdout.strip().split("\n"):
        if not line.strip():
            continue
        parts = line.split("\t")
        if len(parts) < 16:
            continue
        devices.append({
            "splynx_id": int(parts[0]),
            "name": parts[1].strip(),
            "ip": parts[2].strip(),
            "model": parts[3].strip() or None,
            "parent_id": int(parts[4]) if parts[4] and parts[4] != "0" else None,
            "snmp_community": parts[5].strip() or "public",
            "snmp_version": parts[6].strip() or "2c",
            "snmp_port": int(parts[7]) if parts[7] else 161,
            "is_ping": parts[8] == "1",
            "send_notifications": parts[9] == "1",
            "delay_timer": int(parts[10]) if parts[10] and parts[10] != "NULL" else 0,
            "ping_state": parts[11].strip(),
            "snmp_state": parts[12].strip(),
            "type_name": parts[13].strip(),
            "vendor_name": parts[14].strip(),
            "site_name": parts[15].strip() if parts[15] != "NULL" else None,
        })

    return devices


def import_devices(devices: list[dict], dry_run: bool = False) -> dict[str, int]:
    """Import Splynx monitoring devices into DotMac Sub."""
    # Late imports to avoid loading the full app on --help
    from app.db import SessionLocal
    from app.models.network_monitoring import (
        DeviceRole,
        DeviceStatus,
        DeviceType,
        NetworkDevice,
    )

    db = SessionLocal()
    created = 0
    skipped = 0
    updated = 0
    errors = 0

    # Pre-load existing devices by IP for dedup
    existing_by_ip: dict[str, NetworkDevice] = {}
    existing_by_splynx_id: dict[int, NetworkDevice] = {}
    for dev in db.query(NetworkDevice).all():
        if dev.mgmt_ip:
            existing_by_ip[dev.mgmt_ip] = dev
        if dev.splynx_monitoring_id:
            existing_by_splynx_id[dev.splynx_monitoring_id] = dev

    for d in devices:
        ip = d["ip"]
        splynx_id = d["splynx_id"]
        device_type_str = TYPE_MAP.get(d["type_name"], "other")
        vendor_str = VENDOR_MAP.get(d["vendor_name"], d["vendor_name"].lower() if d["vendor_name"] else "other")
        role_str = ROLE_MAP.get(device_type_str, "edge")

        try:
            # Check if already imported (by splynx_id or IP)
            existing = existing_by_splynx_id.get(splynx_id) or existing_by_ip.get(ip)

            if existing:
                # Update fields
                changed = False
                if not existing.splynx_monitoring_id:
                    existing.splynx_monitoring_id = splynx_id
                    changed = True
                if d["model"] and not existing.model:
                    existing.model = d["model"]
                    changed = True
                if d["vendor_name"] and not existing.vendor:
                    existing.vendor = vendor_str
                    changed = True
                if changed:
                    updated += 1
                    if not dry_run:
                        logger.info("  Updated: %s (%s) — splynx_id=%d", existing.name, ip, splynx_id)
                else:
                    skipped += 1
                continue

            if dry_run:
                logger.info("  [DRY RUN] Would create: %s (%s) type=%s vendor=%s", d["name"], ip, device_type_str, vendor_str)
                created += 1
                continue

            # Create new device
            device = NetworkDevice(
                name=d["name"],
                hostname=d["name"],
                mgmt_ip=ip,
                vendor=vendor_str,
                model=d["model"],
                device_type=DeviceType(device_type_str),
                role=DeviceRole(role_str),
                ping_enabled=d["is_ping"],
                snmp_enabled=bool(d["snmp_community"]),
                snmp_community=d["snmp_community"],
                snmp_version=d["snmp_version"],
                snmp_port=d["snmp_port"],
                send_notifications=d["send_notifications"],
                notification_delay_minutes=d["delay_timer"],
                splynx_monitoring_id=splynx_id,
                status=DeviceStatus.online if d["ping_state"] == "up" else DeviceStatus.offline,
                notes=f"Imported from Splynx monitoring (id={splynx_id})",
                is_active=True,
            )
            db.add(device)
            created += 1
            logger.info("  Created: %s (%s) type=%s vendor=%s", d["name"], ip, device_type_str, vendor_str)

        except Exception as exc:
            errors += 1
            logger.warning("  Error importing %s (%s): %s", d["name"], ip, exc)

    if not dry_run:
        db.commit()
        logger.info("Committed %d new devices", created)
    else:
        logger.info("[DRY RUN] Would create %d devices", created)

    db.close()
    return {"created": created, "updated": updated, "skipped": skipped, "errors": errors}


def main():
    parser = argparse.ArgumentParser(description="Import Splynx monitoring devices")
    parser.add_argument("--dry-run", action="store_true", help="Preview without creating records")
    args = parser.parse_args()

    logger.info("Fetching devices from Splynx (138.68.165.175)...")
    devices = fetch_splynx_devices()
    logger.info("Found %d active monitored devices in Splynx", len(devices))

    # Summary by type
    from collections import Counter
    by_type = Counter(d["type_name"] for d in devices)
    by_vendor = Counter(d["vendor_name"] for d in devices)
    logger.info("By type: %s", dict(by_type))
    logger.info("By vendor: %s", dict(by_vendor))

    logger.info("Importing into DotMac Sub%s...", " (DRY RUN)" if args.dry_run else "")
    result = import_devices(devices, dry_run=args.dry_run)
    logger.info("Done: created=%d updated=%d skipped=%d errors=%d",
                result["created"], result["updated"], result["skipped"], result["errors"])


if __name__ == "__main__":
    main()
