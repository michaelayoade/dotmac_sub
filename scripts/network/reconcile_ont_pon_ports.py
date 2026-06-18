"""Reconcile ONT PonPort pointers from imported OLT registrations.

Dry-run is the default:
    poetry run python -m scripts.reconcile_ont_pon_ports

Apply changes:
    poetry run python -m scripts.reconcile_ont_pon_ports --apply
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
    parser.add_argument("--apply", action="store_true", help="Apply the repair")
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
            apply=args.apply,
        )
        if args.apply:
            db.commit()
        else:
            db.rollback()

    logger.info("Mode: %s", "apply" if result.apply else "dry-run")
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

    if not args.apply:
        logger.info(
            "Dry-run only. Re-run with --apply to update ONT and active assignment PonPort pointers."
        )


if __name__ == "__main__":
    main()
