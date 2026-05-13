#!/usr/bin/env python
"""Report missing imported ONT type profile mappings by OLT.

Usage:
    python scripts/report_missing_olt_mappings.py --all
    python scripts/report_missing_olt_mappings.py --olt-name boi-olt
    python scripts/report_missing_olt_mappings.py --all --fail-on-missing
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.db import SessionLocal
from app.services.network.olt_mapping_report import (
    OltMappingCoverage,
    build_olt_mapping_coverage_report,
)


def _coverage_to_dict(item: OltMappingCoverage) -> dict[str, object]:
    return {
        "olt_id": item.olt_id,
        "olt_name": item.olt_name,
        "mapped_equipment_count": item.mapped_equipment_count,
        "observed_equipment_count": item.observed_equipment_count,
        "missing_count": item.missing_count,
        "missing": [
            {
                "equipment_id": missing.equipment_id,
                "inventory_count": missing.inventory_count,
                "imported_registration_count": missing.imported_registration_count,
                "total_count": missing.total_count,
            }
            for missing in item.missing
        ],
    }


def _print_text(report: list[OltMappingCoverage]) -> None:
    total_missing = sum(item.missing_count for item in report)
    print(
        f"OLT mapping coverage: {len(report)} OLT(s), {total_missing} missing mapping(s)"
    )
    for item in report:
        status = "OK" if item.is_complete else f"MISSING {item.missing_count}"
        print(
            f"\n{item.olt_name} [{status}] "
            f"observed={item.observed_equipment_count} mapped={item.mapped_equipment_count}"
        )
        for missing in item.missing:
            print(
                "  - "
                f"{missing.equipment_id}: "
                f"inventory={missing.inventory_count}, "
                f"imported={missing.imported_registration_count}, "
                f"total={missing.total_count}"
            )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    target = parser.add_mutually_exclusive_group(required=True)
    target.add_argument("--all", action="store_true", help="Report all active OLTs")
    target.add_argument("--olt-id", help="Report one OLT by UUID")
    target.add_argument("--olt-name", help="Report one OLT by name")
    parser.add_argument(
        "--include-inactive",
        action="store_true",
        help="Include inactive OLTs in the report",
    )
    parser.add_argument("--json", action="store_true", help="Emit JSON")
    parser.add_argument(
        "--fail-on-missing",
        action="store_true",
        help="Exit 1 when any missing mapping is found",
    )
    args = parser.parse_args()

    db = SessionLocal()
    try:
        report = build_olt_mapping_coverage_report(
            db,
            olt_id=args.olt_id,
            olt_name=args.olt_name,
            active_only=not args.include_inactive,
        )
    finally:
        db.close()

    if args.json:
        print(json.dumps([_coverage_to_dict(item) for item in report], indent=2))
    else:
        _print_text(report)

    has_missing = any(not item.is_complete for item in report)
    return 1 if args.fail_on_missing and has_missing else 0


if __name__ == "__main__":
    raise SystemExit(main())
