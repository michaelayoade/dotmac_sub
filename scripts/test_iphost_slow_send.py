#!/usr/bin/env python3
"""Test IPHOST configuration with slow send fix."""

import sys

sys.path.insert(0, "/opt/dotmac_sub")

from sqlalchemy import select

from app.db import SessionLocal
from app.models.network import OLTDevice, OntUnit
from app.services.network.olt_ssh_ont import configure_ont_iphost


def main():
    db = SessionLocal()
    try:
        # Get Gudu OLT
        stmt = select(OLTDevice).where(OLTDevice.name.ilike("%gudu%"))
        olt = db.scalars(stmt).first()
        if not olt:
            print("Gudu OLT not found")
            return

        print(f"Testing on OLT: {olt.name} ({olt.mgmt_ip})")

        # Get ONTs on board 0/2 with management IPs
        stmt = select(OntUnit).where(
            OntUnit.olt_device_id == olt.id,
            OntUnit.board == "0/2",
            OntUnit.mgmt_ip_address.isnot(None),
            OntUnit.external_id.isnot(None),
        ).limit(3)

        onts = db.scalars(stmt).all()
        if not onts:
            print("No ONTs found on board 0/2 with management IPs")
            return

        print(f"\nTesting {len(onts)} ONTs on board 0/2 with slow send fix...\n")

        success_count = 0
        for ont in onts:
            # Board is already "0/2", so fsp is just "board/port"
            fsp = f"{ont.board}/{ont.port}"

            # Extract ONT ID from external_id which is like "huawei:4194312192.11"
            ext_id = str(ont.external_id)
            if "." in ext_id:
                ont_id = int(ext_id.split(".")[-1])
            else:
                try:
                    ont_id = int(ext_id)
                except ValueError:
                    print(f"Skipping {ont.serial_number} - invalid external_id: {ext_id}")
                    continue

            ip = str(ont.mgmt_ip_address)

            # Get management VLAN from profile
            vlan = 201  # Default management VLAN
            if ont.provisioning_profile and ont.provisioning_profile.mgmt_vlan:
                vlan = ont.provisioning_profile.mgmt_vlan

            port_num = ont.port if ont.port else "0"
            print(f"{ont.serial_number} @ {fsp} ONT-{ont_id}: {ip}")
            print(f"  Command: ont ipconfig {port_num} {ont_id} ip-index 0 static ip-address {ip} mask 255.255.255.0 gateway 172.16.205.1 vlan {vlan}")

            ok, msg = configure_ont_iphost(
                olt,
                fsp,
                ont_id,
                vlan_id=vlan,
                ip_mode="static",
                ip_address=ip,
                subnet="255.255.255.0",
                gateway="172.16.205.1",
            )

            status = "OK" if ok else "FAIL"
            print(f"  Result: {status}: {msg}")
            print()

            if ok:
                success_count += 1

        print(f"\nSuccess: {success_count}/{len(onts)}")

    finally:
        db.close()

if __name__ == "__main__":
    main()
