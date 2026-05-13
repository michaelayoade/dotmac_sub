#!/usr/bin/env python
"""Populate OLT config_pack JSON with traffic table indices and WAN profiles.

Values extracted from running configs in /root/dotmac-olt-configs/2026-04-17/.

Usage:
    poetry run python scripts/migration/populate_olt_config_packs.py
    poetry run python scripts/migration/populate_olt_config_packs.py --apply
"""

from __future__ import annotations

import argparse
import sys
from dataclasses import dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from sqlalchemy import select

from app.db import SessionLocal
from app.models.network import OLTDevice

# Sentinel to indicate "explicitly set to None/unset"
UNSET = object()


@dataclass
class OltConfigUpdate:
    """Config pack updates for an OLT."""

    # Traffic table indices for VLAN 203 (internet)
    internet_traffic_table_inbound: int | None = None
    internet_traffic_table_outbound: int | None = None
    # Traffic table indices for VLAN 201 (management)
    mgmt_traffic_table_inbound: int | None = None
    mgmt_traffic_table_outbound: int | None = None
    # WAN config profile ID (for OMCI-first routing)
    # None = no wan-profile defined, 0+ = valid profile ID
    # UNSET = don't change current value
    wan_config_profile_id: int | None | object = UNSET


# Data extracted from /root/dotmac-olt-configs/2026-04-17/*.cfg
# Keys are lowercase OLT name patterns to match
OLT_CONFIG_UPDATES: dict[str, OltConfigUpdate] = {
    "boi": OltConfigUpdate(
        internet_traffic_table_inbound=26,
        internet_traffic_table_outbound=27,
        mgmt_traffic_table_inbound=25,
        mgmt_traffic_table_outbound=25,
        wan_config_profile_id=None,  # No wan-profile defined on this OLT
    ),
    "jabi": OltConfigUpdate(
        internet_traffic_table_inbound=87,
        internet_traffic_table_outbound=88,
        mgmt_traffic_table_inbound=86,
        mgmt_traffic_table_outbound=86,
        wan_config_profile_id=None,  # No wan-profile defined on this OLT
    ),
    "gudu": OltConfigUpdate(
        internet_traffic_table_inbound=71,
        internet_traffic_table_outbound=72,
        mgmt_traffic_table_inbound=70,
        mgmt_traffic_table_outbound=70,
        wan_config_profile_id=None,  # No wan-profile defined on this OLT
    ),
    "gwarimpa": OltConfigUpdate(
        internet_traffic_table_inbound=7,
        internet_traffic_table_outbound=7,
        mgmt_traffic_table_inbound=86,
        mgmt_traffic_table_outbound=86,
        wan_config_profile_id=0,  # profile-id 0 "smartolt"
    ),
    "karsana": OltConfigUpdate(
        internet_traffic_table_inbound=87,
        internet_traffic_table_outbound=88,
        mgmt_traffic_table_inbound=86,
        mgmt_traffic_table_outbound=86,
        wan_config_profile_id=None,  # No wan-profile defined on this OLT
    ),
    "spdc": OltConfigUpdate(
        internet_traffic_table_inbound=77,
        internet_traffic_table_outbound=78,
        mgmt_traffic_table_inbound=76,
        mgmt_traffic_table_outbound=76,
        wan_config_profile_id=0,  # profile-id 0 "smartolt"
    ),
    "garki": OltConfigUpdate(
        # Values need verification - config dump was binary/incomplete
        internet_traffic_table_inbound=None,
        internet_traffic_table_outbound=None,
        mgmt_traffic_table_inbound=None,
        mgmt_traffic_table_outbound=None,
        wan_config_profile_id=None,  # Needs verification
    ),
}


def find_olt_match(olt_name: str) -> str | None:
    """Find matching OLT config key from OLT name."""
    name_lower = olt_name.lower()
    for key in OLT_CONFIG_UPDATES:
        if key in name_lower:
            return key
    return None


def update_config_pack(existing_pack: dict | None, updates: OltConfigUpdate) -> dict:
    """Merge updates into existing config pack."""
    pack = dict(existing_pack) if existing_pack else {}

    # Only update fields that have values in the update
    if updates.internet_traffic_table_inbound is not None:
        pack["internet_traffic_table_inbound"] = updates.internet_traffic_table_inbound
    if updates.internet_traffic_table_outbound is not None:
        pack["internet_traffic_table_outbound"] = (
            updates.internet_traffic_table_outbound
        )
    if updates.mgmt_traffic_table_inbound is not None:
        pack["mgmt_traffic_table_inbound"] = updates.mgmt_traffic_table_inbound
    if updates.mgmt_traffic_table_outbound is not None:
        pack["mgmt_traffic_table_outbound"] = updates.mgmt_traffic_table_outbound
    # wan_config_profile_id: UNSET = don't change, None = remove, 0+ = set
    if updates.wan_config_profile_id is not UNSET:
        if updates.wan_config_profile_id is None:
            pack.pop("wan_config_profile_id", None)  # Remove if present
        else:
            pack["wan_config_profile_id"] = updates.wan_config_profile_id

    return pack


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--apply",
        action="store_true",
        help="Apply changes to database (default is dry run)",
    )
    args = parser.parse_args()

    db = SessionLocal()
    try:
        stmt = select(OLTDevice).where(OLTDevice.is_active.is_(True))
        olts = list(db.scalars(stmt))

        print(f"Found {len(olts)} active OLTs\n")

        changes = []
        skipped = []

        for olt in olts:
            match_key = find_olt_match(olt.name or olt.hostname or "")
            if not match_key:
                skipped.append(f"  {olt.name}: no matching config")
                continue

            updates = OLT_CONFIG_UPDATES[match_key]
            current_pack = olt.config_pack or {}
            new_pack = update_config_pack(current_pack, updates)

            # Check if there are actual changes
            changed_fields = []
            for field in [
                "internet_traffic_table_inbound",
                "internet_traffic_table_outbound",
                "mgmt_traffic_table_inbound",
                "mgmt_traffic_table_outbound",
                "wan_config_profile_id",
            ]:
                old_val = current_pack.get(field)
                new_val = new_pack.get(field)
                if old_val != new_val:
                    changed_fields.append(f"{field}: {old_val} → {new_val}")

            if not changed_fields:
                skipped.append(f"  {olt.name} ({match_key}): already up to date")
                continue

            changes.append((olt, new_pack, changed_fields))
            print(f"OLT: {olt.name} (matched: {match_key})")
            for change in changed_fields:
                print(f"  {change}")
            print()

        if skipped:
            print("Skipped:")
            for msg in skipped:
                print(msg)
            print()

        if not changes:
            print("No changes needed.")
            return 0

        print(f"Total: {len(changes)} OLTs to update")

        if not args.apply:
            print("\nDry run - use --apply to commit changes")
            return 0

        print("\nApplying changes...")
        updated = 0
        for olt, new_pack, changed_fields in changes:
            try:
                olt.config_pack = new_pack
                db.commit()
                updated += 1
                print(f"  ✓ {olt.name}")
            except Exception as e:
                db.rollback()
                print(f"  ✗ {olt.name}: {e}")
        print(f"\nUpdated {updated}/{len(changes)} OLTs")
        return 0

    finally:
        db.close()


if __name__ == "__main__":
    sys.exit(main())
