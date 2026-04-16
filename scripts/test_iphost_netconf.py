#!/usr/bin/env python3
"""Test IPHOST configuration via NETCONF (preferred) with SSH fallback."""

import sys
sys.path.insert(0, "/opt/dotmac_sub")

from app.db import SessionLocal
from app.models.network import OLTDevice, OntUnit
from app.services.network.olt_ssh_ont import configure_ont_iphost
from sqlalchemy import select


def main():
    db = SessionLocal()
    try:
        # Check which OLTs have NETCONF enabled
        olts = db.scalars(select(OLTDevice).where(OLTDevice.is_active == True)).all()
        print("=== OLT NETCONF Status ===")
        for olt in olts:
            nc_status = "ENABLED" if olt.netconf_enabled else "disabled"
            nc_port = olt.netconf_port or 830
            print(f"{olt.name}: NETCONF {nc_status} (port {nc_port})")

        print()

        # Test on first OLT with NETCONF enabled and ONTs
        for olt in olts:
            if not olt.netconf_enabled:
                continue
            if not olt.ssh_username or not olt.ssh_password:
                continue

            # Get ONTs with management IPs on this OLT
            stmt = select(OntUnit).where(
                OntUnit.olt_device_id == olt.id,
                OntUnit.is_active == True,
                OntUnit.mgmt_ip_address.isnot(None),
                OntUnit.external_id.isnot(None),
            ).limit(3)

            onts = db.scalars(stmt).all()
            if not onts:
                print(f"{olt.name}: No ONTs with management IPs")
                continue

            print(f"\n=== Testing IPHOST on {olt.name} ({len(onts)} ONTs) ===")

            success_count = 0
            for ont in onts:
                fsp = f"{ont.board}/{ont.port}"

                # Extract ONT ID from external_id
                ext_id = str(ont.external_id)
                if "." in ext_id:
                    ont_id = int(ext_id.split(".")[-1])
                else:
                    try:
                        ont_id = int(ext_id)
                    except ValueError:
                        print(f"  {ont.serial_number}: Invalid external_id {ext_id}")
                        continue

                ip = str(ont.mgmt_ip_address)
                vlan = 201
                if ont.provisioning_profile and ont.provisioning_profile.mgmt_vlan:
                    vlan = ont.provisioning_profile.mgmt_vlan

                print(f"  {ont.serial_number} @ {fsp} ONT-{ont_id}: {ip} VLAN {vlan}")

                ok, msg = configure_ont_iphost(
                    olt,
                    fsp,
                    ont_id,
                    vlan_id=vlan,
                    ip_mode="static",
                    ip_address=ip,
                    subnet="255.255.255.0",
                    gateway="172.16.205.1",  # Adjust per OLT if needed
                )

                status = "OK" if ok else "FAIL"
                print(f"    -> {status}: {msg}")

                if ok:
                    success_count += 1

            print(f"\n  Success: {success_count}/{len(onts)}")
            break  # Only test first OLT with NETCONF

    finally:
        db.close()


if __name__ == "__main__":
    main()
