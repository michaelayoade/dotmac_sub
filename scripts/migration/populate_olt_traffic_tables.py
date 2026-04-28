"""Populate OLT traffic table indices from running config analysis.

Data extracted from /root/dotmac-olt-configs/2026-04-17/*.cfg

Usage:
    poetry run python scripts/migration/populate_olt_traffic_tables.py
    poetry run python scripts/migration/populate_olt_traffic_tables.py --apply
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from sqlalchemy import select

from app.db import SessionLocal
from app.models.network import OLTDevice

# Traffic table indices extracted from OLT running configs (2026-04-26)
# Format: olt_name_prefix -> (mgmt_in, mgmt_out, internet_in, internet_out)
TRAFFIC_TABLE_DATA: dict[str, tuple[int, int, int, int]] = {
    "boi": (25, 25, 26, 27),
    "garki": (86, 86, 87, 88),
    "gudu": (70, 70, 71, 72),
    "gwarimpa": (86, 86, 87, 88),
    "jabi": (86, 86, 87, 88),
    "karsana": (86, 86, 87, 88),
    "spdc": (76, 76, 77, 78),
}


def _olt_key(name: str) -> str:
    """Extract OLT key from name (first word, lowercased)."""
    return (name or "").lower().split()[0] if name else ""


def populate_traffic_tables(*, apply: bool = False) -> dict[str, int]:
    """Populate traffic table indices on OLT devices.

    Args:
        apply: If True, commit changes. Otherwise dry-run.

    Returns:
        Stats dict with counts.
    """
    db = SessionLocal()
    stats = {"olts_found": 0, "olts_matched": 0, "olts_updated": 0, "olts_skipped": 0}

    try:
        olts = db.scalars(select(OLTDevice).where(OLTDevice.is_active.is_(True))).all()
        stats["olts_found"] = len(olts)

        for olt in olts:
            key = _olt_key(olt.name)
            if key not in TRAFFIC_TABLE_DATA:
                print(f"SKIP: {olt.name} (no traffic table data for key '{key}')")
                stats["olts_skipped"] += 1
                continue

            mgmt_in, mgmt_out, inet_in, inet_out = TRAFFIC_TABLE_DATA[key]
            stats["olts_matched"] += 1

            # Check if already set
            already_set = (
                olt.mgmt_traffic_table_inbound == mgmt_in
                and olt.mgmt_traffic_table_outbound == mgmt_out
                and olt.internet_traffic_table_inbound == inet_in
                and olt.internet_traffic_table_outbound == inet_out
            )

            if already_set:
                print(f"OK:   {olt.name} (already configured)")
                continue

            print(
                f"SET:  {olt.name} <- mgmt={mgmt_in}/{mgmt_out}, "
                f"internet={inet_in}/{inet_out}"
            )

            if apply:
                olt.mgmt_traffic_table_inbound = mgmt_in
                olt.mgmt_traffic_table_outbound = mgmt_out
                olt.internet_traffic_table_inbound = inet_in
                olt.internet_traffic_table_outbound = inet_out
                stats["olts_updated"] += 1

        if apply:
            db.commit()
            print(f"\nCommitted {stats['olts_updated']} OLT updates.")
        else:
            db.rollback()
            print("\nDRY-RUN: No changes committed. Use --apply to commit.")

        return stats

    except Exception:
        db.rollback()
        raise
    finally:
        db.close()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--apply",
        action="store_true",
        help="Commit changes (default is dry-run)",
    )
    args = parser.parse_args()

    print("=" * 60)
    print("Populate OLT Traffic Table Indices")
    print("=" * 60)
    print()

    stats = populate_traffic_tables(apply=args.apply)

    print()
    print("Summary:")
    for key, value in stats.items():
        print(f"  {key}: {value}")


if __name__ == "__main__":
    main()
