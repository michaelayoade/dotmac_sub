#!/usr/bin/env python3
"""Fix service-port allocations that are assigned to wrong ONTs.

The issue: When multiple ONTs share the same FSP (Frame/Slot/Port),
allocations were incorrectly assigned based on FSP alone instead of FSP+ONT-ID.

This script:
1. Parses OLT config files to get the correct mapping:
   - service-port index → (FSP, ONT-ID)
   - (FSP, ONT-ID) → hex serial
2. For each allocation, verify it's assigned to the ONT with matching serial
3. Reassign allocations from wrong ONTs to correct ones
"""

from __future__ import annotations

import re
import sys
from pathlib import Path
from uuid import UUID

# Add app to path
sys.path.insert(0, str(Path(__file__).parent.parent))

from sqlalchemy import select

from app.db import SessionLocal
from app.models.network import OLTDevice, OltServicePortPool, OntUnit, ServicePortAllocation


def parse_config(config_content: str) -> tuple[dict, dict]:
    """Parse OLT config to extract mappings.

    Returns:
        - location_to_hex: (FSP, ONT-ID) → hex serial
        - sp_to_location: port_index → (FSP, ONT-ID, VLAN, GEM)
    """
    interface_pattern = re.compile(r"interface gpon (\d+/\d+)")
    ont_pattern = re.compile(r'ont add (\d+)\s+(\d+)\s+sn-auth\s+"([A-Fa-f0-9]+)"')
    sp_pattern = re.compile(
        r'service-port\s+(\d+)\s+vlan\s+(\d+)\s+gpon\s+(\d+/\d+/\d+)\s+ont\s+(\d+)\s+gemport\s+(\d+)'
    )

    # Parse ONT registrations
    current_frame_slot = None
    location_to_hex = {}

    for line in config_content.split('\n'):
        line = line.strip()

        interface_match = interface_pattern.search(line)
        if interface_match:
            current_frame_slot = interface_match.group(1)
            continue

        if current_frame_slot:
            ont_match = ont_pattern.search(line)
            if ont_match:
                port = int(ont_match.group(1))
                ont_id = int(ont_match.group(2))
                hex_serial = ont_match.group(3).upper()

                fsp = f"{current_frame_slot}/{port}"
                location_to_hex[(fsp, ont_id)] = hex_serial

    # Parse service-ports
    sp_to_location = {}
    for match in sp_pattern.finditer(config_content):
        sp_index = int(match.group(1))
        vlan = int(match.group(2))
        fsp = match.group(3)
        ont_id = int(match.group(4))
        gem = int(match.group(5))
        sp_to_location[sp_index] = (fsp, ont_id, vlan, gem)

    return location_to_hex, sp_to_location


def find_ont_by_hex_serial(onts: list[OntUnit], hex_serial: str) -> OntUnit | None:
    """Find ONT by matching the hex serial suffix."""
    # The hex serial is 16 chars (8 bytes). First 4 bytes are vendor code (HWTC = 48575443)
    # Last 4 bytes are the unique part
    suffix = hex_serial[-8:].upper()  # Last 4 bytes as hex

    for ont in onts:
        if not ont.serial_number:
            continue

        ont_serial = ont.serial_number.upper()

        # Match patterns:
        # 1. Serial like "HWTC0030C592" - last 8 chars are the suffix
        # 2. Serial like "HW-xxx-yyy" - generated, won't match

        if ont_serial.startswith("HWTC"):
            ont_suffix = ont_serial[4:].upper()
            # Direct match
            if ont_suffix == suffix:
                return ont
            # Partial match (some serials might be truncated)
            if len(ont_suffix) >= 6 and suffix.endswith(ont_suffix[-6:]):
                return ont

        # Also check if hex suffix appears anywhere (fallback)
        if suffix in ont_serial:
            return ont

    return None


def fix_allocations_for_olt(
    db,
    olt: OLTDevice,
    config_path: Path,
    dry_run: bool = True,
):
    """Fix service-port allocations for a single OLT."""
    print(f"\n{'='*60}")
    print(f"OLT: {olt.name}")
    print(f"Config: {config_path}")
    print(f"{'='*60}")

    config_content = config_path.read_text(errors="replace")
    location_to_hex, sp_to_location = parse_config(config_content)

    print(f"Parsed {len(location_to_hex)} ONT registrations")
    print(f"Parsed {len(sp_to_location)} service-port definitions")

    # Get pool
    pool = db.scalars(
        select(OltServicePortPool).where(
            OltServicePortPool.olt_device_id == olt.id,
            OltServicePortPool.is_active.is_(True),
        )
    ).first()

    if not pool:
        print("  No service-port pool found")
        return {"fixes": 0, "created": 0}

    # Get all allocations
    allocations = db.scalars(
        select(ServicePortAllocation).where(
            ServicePortAllocation.pool_id == pool.id,
            ServicePortAllocation.is_active.is_(True),
        )
    ).all()

    allocations_by_index = {a.port_index: a for a in allocations}
    print(f"Found {len(allocations)} existing allocations")

    # Get all ONTs for this OLT
    onts = db.scalars(select(OntUnit).where(OntUnit.olt_device_id == olt.id)).all()
    print(f"Found {len(onts)} ONTs in database")

    fixes = []
    creates = []

    for sp_index, (fsp, ont_id, vlan, gem) in sp_to_location.items():
        # Get expected hex serial
        hex_serial = location_to_hex.get((fsp, ont_id))
        if not hex_serial:
            continue

        # Find correct ONT
        correct_ont = find_ont_by_hex_serial(onts, hex_serial)
        if not correct_ont:
            continue

        # Check allocation
        existing = allocations_by_index.get(sp_index)

        if existing:
            if existing.ont_unit_id != correct_ont.id:
                # Wrong ONT
                wrong_ont = db.get(OntUnit, existing.ont_unit_id)
                fixes.append({
                    "index": sp_index,
                    "allocation_id": existing.id,
                    "wrong_ont_id": existing.ont_unit_id,
                    "wrong_serial": wrong_ont.serial_number if wrong_ont else "?",
                    "correct_ont_id": correct_ont.id,
                    "correct_serial": correct_ont.serial_number,
                    "vlan": vlan,
                    "gem": gem,
                })
        else:
            # Missing allocation
            creates.append({
                "index": sp_index,
                "ont_id": correct_ont.id,
                "serial": correct_ont.serial_number,
                "vlan": vlan,
                "gem": gem,
            })

    # Report
    print(f"\nWrong allocations to fix: {len(fixes)}")
    for fix in fixes[:5]:
        print(f"  Index {fix['index']}: {fix['wrong_serial'][:25]}")
        print(f"         → {fix['correct_serial']}")
    if len(fixes) > 5:
        print(f"  ... and {len(fixes) - 5} more")

    print(f"\nMissing allocations to create: {len(creates)}")
    for c in creates[:5]:
        print(f"  Index {c['index']}: {c['serial']} VLAN {c['vlan']}")
    if len(creates) > 5:
        print(f"  ... and {len(creates) - 5} more")

    if dry_run:
        print("\n[DRY RUN] No changes made")
        return {"fixes": len(fixes), "created": len(creates)}

    # Apply fixes
    from datetime import UTC, datetime

    for fix in fixes:
        alloc = db.get(ServicePortAllocation, fix["allocation_id"])
        if alloc:
            alloc.ont_unit_id = fix["correct_ont_id"]
            alloc.vlan_id = fix["vlan"]
            alloc.gem_index = fix["gem"]

    # Create missing allocations
    for c in creates:
        alloc = ServicePortAllocation(
            pool_id=pool.id,
            ont_unit_id=c["ont_id"],
            port_index=c["index"],
            vlan_id=c["vlan"],
            gem_index=c["gem"],
            is_active=True,
            provisioned_at=datetime.now(UTC),
        )
        db.add(alloc)

    db.commit()
    print(f"\nApplied {len(fixes)} fixes and created {len(creates)} allocations")
    return {"fixes": len(fixes), "created": len(creates)}


def main():
    import argparse

    parser = argparse.ArgumentParser(description="Fix service-port allocations")
    parser.add_argument("--dry-run", action="store_true", default=True,
                        help="Don't make changes (default)")
    parser.add_argument("--execute", action="store_true",
                        help="Actually make changes")
    parser.add_argument("--olt", type=str, help="OLT name filter")
    args = parser.parse_args()

    dry_run = not args.execute

    # OLT name → config file mapping
    config_dir = Path("/root/dotmac-olt-configs/2026-04-17")
    olt_configs = {
        "BOI Huawei OLT": config_dir / "boi.cfg",
        "Jabi Huawei OLT": config_dir / "jabi.cfg",
        "Karsana Huawei OLT": config_dir / "karsana.cfg",
        "Gwarimpa Huawei OLT": config_dir / "gwarimpa.cfg",
        "SPDC Huawei OLT": config_dir / "spdc.cfg",
        "Garki Huawei OLT": config_dir / "garki.cfg",
        "Gudu Huawei OLT": config_dir / "gudu.cfg",
    }

    db = SessionLocal()
    totals = {"fixes": 0, "created": 0}

    try:
        olts = db.scalars(select(OLTDevice)).all()

        for olt in olts:
            if args.olt and args.olt.lower() not in olt.name.lower():
                continue

            config_path = olt_configs.get(olt.name)
            if not config_path or not config_path.exists():
                print(f"No config file for {olt.name}")
                continue

            result = fix_allocations_for_olt(db, olt, config_path, dry_run=dry_run)
            totals["fixes"] += result["fixes"]
            totals["created"] += result["created"]

        print(f"\n{'='*60}")
        print(f"TOTAL: {totals['fixes']} fixes, {totals['created']} creates")
        print(f"{'='*60}")

    finally:
        db.close()


if __name__ == "__main__":
    main()
