"""Backfill pon_port_id on OntUnit from active OntAssignment.

This script copies pon_port_id from each ONT's active assignment to the
OntUnit itself, moving topology data closer to the device.

Usage:
    # Dry-run (default): show what would be updated
    poetry run python -m scripts.migration.backfill_ont_unit_pon_port_id

    # Apply changes
    poetry run python -m scripts.migration.backfill_ont_unit_pon_port_id --apply
"""

from __future__ import annotations

import logging
import sys

from sqlalchemy import select

from scripts.migration.db_connections import dotmac_session

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger(__name__)


def backfill_ont_unit_pon_port_id(dry_run: bool = True) -> None:
    """Copy pon_port_id from active assignment to OntUnit."""
    logger.info("=== Backfill OntUnit.pon_port_id from OntAssignment ===")

    with dotmac_session() as db:
        from app.models.network import OntAssignment, OntUnit

        # Find ONTs with active assignments that have pon_port_id
        stmt = (
            select(OntUnit, OntAssignment.pon_port_id)
            .join(OntAssignment, OntAssignment.ont_unit_id == OntUnit.id)
            .where(
                OntAssignment.active.is_(True),
                OntAssignment.pon_port_id.isnot(None),
            )
        )
        rows = db.execute(stmt).all()

        logger.info("Found %d ONTs with active assignment pon_port_id", len(rows))

        updated = 0
        skipped_already_set = 0
        skipped_mismatch = 0

        for ont, assignment_pon_port_id in rows:
            if ont.pon_port_id is not None:
                if ont.pon_port_id == assignment_pon_port_id:
                    skipped_already_set += 1
                else:
                    # ONT has different pon_port_id than assignment - log warning
                    logger.warning(
                        "ONT %s (%s) has pon_port_id=%s but assignment has %s - skipping",
                        ont.id,
                        ont.serial_number,
                        ont.pon_port_id,
                        assignment_pon_port_id,
                    )
                    skipped_mismatch += 1
                continue

            if not dry_run:
                ont.pon_port_id = assignment_pon_port_id
            updated += 1

        if dry_run:
            logger.info("DRY RUN - no changes made")
            logger.info("  Would update: %d ONTs", updated)
            logger.info("  Already set (matching): %d ONTs", skipped_already_set)
            logger.info("  Skipped (mismatch): %d ONTs", skipped_mismatch)
        else:
            db.commit()
            logger.info("APPLIED changes")
            logger.info("  Updated: %d ONTs", updated)
            logger.info("  Already set (matching): %d ONTs", skipped_already_set)
            logger.info("  Skipped (mismatch): %d ONTs", skipped_mismatch)

        # Count total ONTs and those with pon_port_id
        from sqlalchemy import func

        total_onts = db.scalar(select(func.count(OntUnit.id)))
        onts_with_pon_port = db.scalar(
            select(func.count(OntUnit.id)).where(OntUnit.pon_port_id.isnot(None))
        )
        logger.info(
            "Total ONTs: %d, with pon_port_id: %d",
            total_onts or 0,
            onts_with_pon_port or 0,
        )


if __name__ == "__main__":
    if "--apply" in sys.argv:
        backfill_ont_unit_pon_port_id(dry_run=False)
    else:
        backfill_ont_unit_pon_port_id(dry_run=True)
        print(
            "\nTo apply: poetry run python -m scripts.migration.backfill_ont_unit_pon_port_id --apply"
        )
