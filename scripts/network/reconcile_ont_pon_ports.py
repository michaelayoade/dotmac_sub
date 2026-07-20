"""Report ONT/PON discrepancies from imported OLT registrations.

This command is observation-only. Repairs use the independently reviewed
``scripts.network.review_ont_assignment_identity`` workflow with exact IDs.
"""

from __future__ import annotations

import argparse
import logging

from app.db import SessionLocal
from app.services.network.ont_topology_reconcile import (
    reconcile_ont_pon_ports_from_registrations,
)

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger(__name__)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--olt-id", help="Limit repair to one OLT ID")
    parser.add_argument(
        "--limit",
        type=int,
        default=25,
        help="Number of candidate rows to print",
    )
    args = parser.parse_args()

    with SessionLocal() as db:
        result = reconcile_ont_pon_ports_from_registrations(
            db,
            olt_id=args.olt_id,
        )
        db.rollback()

    logger.info("Mode: observation-only")
    logger.info("Candidates: %d", len(result.candidates))
    logger.info("Updated: %d", result.updated)
    logger.info("Created PonPorts: %d", result.created_pon_ports)
    logger.info("Already correct: %d", result.already_correct)
    logger.info("Missing from DB: %d", result.missing_from_db)
    logger.info("Missing from registration: %d", result.missing_from_registration)
    logger.info("Skipped: %d", result.skipped)

    for candidate in result.candidates[: args.limit]:
        logger.info(
            "serial=%s olt=%s db_fsp=%s olt_fsp=%s created_pon=%s skipped=%s",
            candidate.serial_number,
            candidate.olt_id,
            candidate.current_fsp,
            candidate.registration_fsp,
            candidate.created_pon_port,
            candidate.skipped_reason,
        )

    logger.info(
        "Submit exact discrepancies through the reviewed "
        "network.ont_assignment_identity workflow."
    )


if __name__ == "__main__":
    main()
