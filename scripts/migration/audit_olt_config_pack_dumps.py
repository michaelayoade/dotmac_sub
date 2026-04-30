"""Audit OLT config packs against local running-config dumps.

This validates Dotmac's saved OLT config pack without logging into the OLT.
With --apply-line-profile-suggestions it updates only the local config_pack
line_profile_id when a compatible dumped profile is available.
"""

from __future__ import annotations

import argparse
import json
import logging
import sys

from scripts.migration.db_connections import dotmac_session

logging.basicConfig(level=logging.INFO, format="%(message)s")
logger = logging.getLogger(__name__)


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--olt", help="Audit one OLT by UUID")
    parser.add_argument("--all", action="store_true", help="Audit all active OLTs")
    parser.add_argument("--json", action="store_true", help="Emit JSON output")
    parser.add_argument(
        "--apply-line-profile-suggestions",
        action="store_true",
        help="Update config_pack.line_profile_id from dump-backed suggestions",
    )
    return parser.parse_args()


def main() -> int:
    args = _parse_args()
    if not args.all and not args.olt:
        logger.error("Specify --all or --olt <id>.")
        return 2

    with dotmac_session() as db:
        from app.services.network.olt_config_pack_dump_audit import (
            active_olt_ids,
            apply_dump_audit_suggestions,
            audit_olt_config_pack_dump,
        )

        olt_ids = [args.olt] if args.olt else active_olt_ids(db)
        reports = [audit_olt_config_pack_dump(db, olt_id) for olt_id in olt_ids]
        updated = 0
        if args.apply_line_profile_suggestions:
            updated = apply_dump_audit_suggestions(db, reports)
            if updated:
                reports = [
                    audit_olt_config_pack_dump(db, olt_id) for olt_id in olt_ids
                ]

    if args.json:
        payload = {
            "updated": updated,
            "reports": [report.to_dict() for report in reports],
        }
        print(json.dumps(payload, indent=2))
    else:
        logger.info("=" * 60)
        logger.info("Local Dump OLT Config-Pack Audit")
        logger.info("=" * 60)
        if updated:
            logger.info("Applied %s config-pack line_profile_id update(s).", updated)
        for report in reports:
            status = "VALID" if report.is_valid else "INVALID"
            logger.info("\n%s: %s", report.olt_name, status)
            if report.dump_path:
                logger.info("  DUMP: %s", report.dump_path)
            for error in report.errors:
                logger.info("  ERROR: %s", error)
            for warning in report.warnings:
                logger.info("  WARNING: %s", warning)
            if report.suggested_updates:
                logger.info("  SUGGEST: %s", report.suggested_updates)
            if not report.errors and not report.warnings:
                logger.info("  OK: Dumped OLT profiles match the config pack.")
    return 0 if all(report.is_valid for report in reports) else 1


if __name__ == "__main__":
    sys.exit(main())
