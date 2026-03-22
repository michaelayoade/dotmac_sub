"""Generate MikroTik RouterOS commands to enable SNMP on all devices.

Outputs a script that can be pasted into MikroTik terminal or
executed via The Dude's bulk command feature.

Usage:
    poetry run python scripts/generate_mikrotik_snmp_enable.py
    poetry run python scripts/generate_mikrotik_snmp_enable.py --server-ip 160.119.127.1
"""

from __future__ import annotations

import sys

from sqlalchemy import text

from app.db import SessionLocal


def main() -> None:
    server_ip = "0.0.0.0/0"  # Allow from all by default
    for arg in sys.argv:
        if arg.startswith("--server-ip="):
            server_ip = arg.split("=")[1]

    db = SessionLocal()

    # Get devices that don't have SNMP working (no interfaces with snmp_index)
    rows = db.execute(text(
        "SELECT nd.name, nd.mgmt_ip, nd.vendor "
        "FROM network_devices nd "
        "WHERE nd.snmp_enabled = true AND nd.is_active = true "
        "AND nd.mgmt_ip IS NOT NULL "
        "AND nd.id NOT IN ("
        "  SELECT DISTINCT device_id FROM device_interfaces WHERE snmp_index IS NOT NULL"
        ") "
        "ORDER BY nd.mgmt_ip"
    )).all()

    print("# MikroTik SNMP Enable Script")
    print(f"# {len(rows)} devices need SNMP enabled")
    print(f"# Server IP allowed: {server_ip}")
    print("#")
    print("# Paste into RouterOS terminal or use The Dude bulk commands")
    print("#")
    print()

    # RouterOS command to enable SNMP
    print("# === RouterOS commands (paste into terminal) ===")
    print("/snmp set enabled=yes trap-community=public")
    print(f"/snmp community set 0 name=public addresses={server_ip} read-access=yes write-access=no")
    print()

    # Device list
    print("# === Devices needing SNMP ===")
    mikrotik_count = 0
    other_count = 0
    for r in rows:
        vendor = (r.vendor or "unknown").lower()
        if "mikrotik" in vendor or "routeros" in vendor:
            mikrotik_count += 1
        elif "ubiquiti" in vendor:
            other_count += 1
        else:
            other_count += 1
        print(f"# {r.name} ({r.mgmt_ip}) vendor={r.vendor or 'unknown'}")

    print()
    print(f"# Summary: {mikrotik_count} MikroTik, {other_count} other vendors")
    print(f"# Total: {len(rows)} devices")

    db.close()


if __name__ == "__main__":
    main()
