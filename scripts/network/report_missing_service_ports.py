"""Report ONT registrations missing observed OLT service-port bindings."""

from __future__ import annotations

import argparse
import logging

from app.services.network.olt_service_port_gaps import find_missing_service_ports
from scripts.migration.db_connections import dotmac_session

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger(__name__)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--olt-id", help="Limit report to one OLT ID")
    parser.add_argument("--limit", type=int, default=50)
    args = parser.parse_args()

    with dotmac_session() as db:
        missing = find_missing_service_ports(db, olt_id=args.olt_id)

    logger.info("Missing service-port bindings: %d", len(missing))
    for item in missing[: args.limit]:
        logger.info(
            "olt=%s fsp=%s ont=%s serial=%s line=%s service=%s desc=%s",
            item.olt_id,
            item.fsp,
            item.ont_id_on_olt,
            item.serial_number or "",
            item.line_profile_id,
            item.service_profile_id,
            item.description or "",
        )


if __name__ == "__main__":
    main()
