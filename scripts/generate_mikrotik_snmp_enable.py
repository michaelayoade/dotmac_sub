"""Generate vendor-specific SNMP enable commands for network devices.

Outputs commands for MikroTik RouterOS and Huawei OLTs.
Ubiquiti devices are listed separately (need manual config via web UI).

Usage:
    PYTHONPATH=. poetry run python scripts/generate_mikrotik_snmp_enable.py
    PYTHONPATH=. poetry run python scripts/generate_mikrotik_snmp_enable.py --server-ip 160.119.127.1
"""

from __future__ import annotations

import sys

from sqlalchemy import text

from app.db import SessionLocal


def main() -> None:
    server_ip = "0.0.0.0/0"
    for arg in sys.argv:
        if arg.startswith("--server-ip="):
            server_ip = arg.split("=")[1]

    db = SessionLocal()

    rows = db.execute(
        text(
            "SELECT nd.name, nd.mgmt_ip, COALESCE(nd.vendor, 'unknown') as vendor "
            "FROM network_devices nd "
            "WHERE nd.snmp_enabled = true AND nd.is_active = true "
            "AND nd.mgmt_ip IS NOT NULL "
            "AND nd.id NOT IN ("
            "  SELECT DISTINCT device_id FROM device_interfaces WHERE snmp_index IS NOT NULL"
            ") "
            "ORDER BY nd.vendor, nd.mgmt_ip"
        )
    ).all()

    mikrotik = [r for r in rows if r.vendor.lower() in ("mikrotik", "routeros")]
    huawei = [r for r in rows if r.vendor.lower() == "huawei"]
    ubiquiti = [r for r in rows if r.vendor.lower() == "ubiquiti"]
    other = [r for r in rows if r not in mikrotik + huawei + ubiquiti]

    print("# ============================================================")
    print("# SNMP Enable Script — Vendor-Specific Commands")
    print(f"# {len(rows)} devices need SNMP enabled")
    print(f"# Server IP: {server_ip}")
    print("# ============================================================")
    print()

    # MikroTik
    print(f"# === MikroTik RouterOS ({len(mikrotik)} devices) ===")
    print("# Paste into RouterOS terminal (Winbox > New Terminal):")
    print("/snmp set enabled=yes trap-community=public")
    print(
        f"/snmp community set 0 name=public addresses={server_ip} read-access=yes write-access=no"
    )
    print()
    for r in mikrotik:
        print(f"#   {r.name} ({r.mgmt_ip})")
    print()

    # Huawei OLTs
    print(f"# === Huawei OLTs ({len(huawei)} devices) ===")
    print("# Paste into OLT CLI (SSH or console):")
    print("# system-view")
    print("# snmp-agent")
    print("# snmp-agent community read public acl 2000")
    print("# snmp-agent sys-info version v2c")
    print("# quit")
    print()
    for r in huawei:
        print(f"#   {r.name} ({r.mgmt_ip})")
    print()

    # Ubiquiti
    print(f"# === Ubiquiti ({len(ubiquiti)} devices) ===")
    print("# These are APs/switches — SNMP is configured via web UI:")
    print("#   System > SNMP > Enable SNMP Agent > Community: public")
    print("# NOTE: Ubiquiti APs don't have per-subscriber bandwidth.")
    print("# Bandwidth monitoring should focus on MikroTik NAS devices.")
    print()
    for r in ubiquiti:
        print(f"#   {r.name} ({r.mgmt_ip})")
    print()

    if other:
        print(f"# === Other ({len(other)} devices) ===")
        for r in other:
            print(f"#   {r.name} ({r.mgmt_ip}) vendor={r.vendor}")
        print()

    print("# ============================================================")
    print(f"# Priority: Enable SNMP on {len(mikrotik)} MikroTik NAS devices first")
    print("# These carry PPPoE sessions with per-subscriber bandwidth data")
    print("# ============================================================")

    db.close()


if __name__ == "__main__":
    main()
