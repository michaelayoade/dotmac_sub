"""Run read-only live OLT config-pack audits.

This validates that Dotmac's saved OLT config pack matches the profiles that
exist on the OLT itself. It does not update the OLT or the database.
"""

from __future__ import annotations

import argparse
import json
import logging
import sys

from sqlalchemy import select

from scripts.migration.db_connections import dotmac_session

logging.basicConfig(level=logging.INFO, format="%(message)s")
logger = logging.getLogger(__name__)


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--olt", help="Audit one OLT by UUID")
    parser.add_argument("--all", action="store_true", help="Audit all active OLTs")
    parser.add_argument(
        "--suggest",
        action="store_true",
        help="Also list compatible live line profiles that could fix mismatches",
    )
    parser.add_argument("--json", action="store_true", help="Emit JSON output")
    return parser.parse_args()


def main() -> int:
    args = _parse_args()
    if not args.all and not args.olt:
        logger.error("Specify --all or --olt <id>.")
        return 2

    with dotmac_session() as db:
        from app.models.network import OLTDevice
        from app.services.network.olt_config_pack_live_audit import (
            audit_olt_config_pack_live,
            suggest_compatible_line_profiles,
        )

        if args.olt:
            olt_ids = [args.olt]
        else:
            olt_ids = [
                str(olt_id)
                for olt_id in db.scalars(
                    select(OLTDevice.id)
                    .where(OLTDevice.is_active.is_(True))
                    .order_by(OLTDevice.name)
                ).all()
            ]

        reports = [audit_olt_config_pack_live(db, olt_id) for olt_id in olt_ids]
        suggestions_by_olt = {}
        if args.suggest:
            for olt_id in olt_ids:
                ok, message, suggestions = suggest_compatible_line_profiles(db, olt_id)
                suggestions_by_olt[olt_id] = {
                    "success": ok,
                    "message": message,
                    "suggestions": [suggestion.to_dict() for suggestion in suggestions],
                }

    if args.json:
        payload = []
        for report in reports:
            item = report.to_dict()
            if args.suggest:
                item["compatible_line_profiles"] = suggestions_by_olt.get(
                    report.olt_id,
                    {},
                )
            payload.append(item)
        print(json.dumps(payload, indent=2))
    else:
        logger.info("=" * 60)
        logger.info("Live OLT Config-Pack Audit")
        logger.info("=" * 60)
        for report in reports:
            status = "VALID" if report.is_valid else "INVALID"
            logger.info("\n%s: %s", report.olt_name, status)
            for error in report.errors:
                logger.info("  ERROR: %s", error)
            for warning in report.warnings:
                logger.info("  WARNING: %s", warning)
            if not report.errors and not report.warnings:
                logger.info("  OK: Live OLT profiles match the config pack.")
            if args.suggest:
                suggestion_result = suggestions_by_olt.get(report.olt_id, {})
                suggestions = suggestion_result.get("suggestions", [])
                if not suggestions:
                    logger.info(
                        "  SUGGEST: %s",
                        suggestion_result.get(
                            "message", "No compatible profile found."
                        ),
                    )
                for suggestion in suggestions:
                    logger.info(
                        "  SUGGEST: imported line profile=%s name=%s gems=%s tr069_ip_index=%s bindings=%s",
                        suggestion.get("profile_id"),
                        suggestion.get("name"),
                        suggestion.get("gem_indexes"),
                        suggestion.get("tr069_ip_index"),
                        suggestion.get("binding_count"),
                    )

    return 0 if all(report.is_valid for report in reports) else 1


if __name__ == "__main__":
    sys.exit(main())
