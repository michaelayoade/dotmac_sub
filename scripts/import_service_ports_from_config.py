#!/usr/bin/env python
"""Import service-ports from OLT config files into DB allocator.

Parses Huawei OLT configuration files and creates ServicePortAllocation
records in the database.

Usage:
    poetry run python scripts/import_service_ports_from_config.py /path/to/configs/
    poetry run python scripts/import_service_ports_from_config.py /path/to/configs/ --dry-run
"""

from __future__ import annotations

import argparse
import logging
import re
import sys
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

sys.path.insert(0, "/opt/dotmac_sub")

from sqlalchemy import select

from app.db import SessionLocal
from app.models.network import (
    OLTDevice,
    OltServicePortPool,
    OntAssignment,
    OntUnit,
    ServicePortAllocation,
)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
logger = logging.getLogger(__name__)

# Map config file names to OLT name patterns
OLT_NAME_MAPPING = {
    "boi": "BOI",
    "garki": "Garki",
    "gudu": "Gudu",
    "gwarimpa": "Gwarimpa",
    "jabi": "Jabi",
    "karsana": "Karsana",
    "spdc": "SPDC",
}

# Regex to parse service-port lines
# service-port <index> vlan <vlan_id> gpon <fsp> ont <ont_id> gemport <gem> ...
SERVICE_PORT_RE = re.compile(
    r"service-port\s+(\d+)\s+"  # index
    r"vlan\s+(\d+)\s+"  # vlan_id
    r"(?:gpon|xgpon|epon)\s+(\d+/\d+/\d+)\s+"  # fsp
    r"ont\s+(\d+)\s+"  # ont_id
    r"gemport\s+(\d+)"  # gemport
)


@dataclass
class ParsedServicePort:
    """Parsed service-port entry from config."""

    index: int
    vlan_id: int
    fsp: str
    ont_id: int
    gemport: int


def parse_config_file(filepath: Path) -> list[ParsedServicePort]:
    """Parse service-port entries from a config file."""
    results = []
    try:
        content = filepath.read_text(encoding="utf-8", errors="replace")
    except Exception as e:
        logger.error("Failed to read %s: %s", filepath, e)
        return results

    for line in content.splitlines():
        line = line.strip()
        if not line.startswith("service-port"):
            continue

        match = SERVICE_PORT_RE.search(line)
        if match:
            results.append(
                ParsedServicePort(
                    index=int(match.group(1)),
                    vlan_id=int(match.group(2)),
                    fsp=match.group(3),
                    ont_id=int(match.group(4)),
                    gemport=int(match.group(5)),
                )
            )

    return results


def find_olt_by_name_pattern(db, pattern: str) -> OLTDevice | None:
    """Find an OLT whose name contains the pattern (case-insensitive)."""
    stmt = select(OLTDevice).where(
        OLTDevice.is_active == True,
        OLTDevice.name.ilike(f"%{pattern}%"),
    )
    return db.scalars(stmt).first()


def _extract_ont_id_from_external(external_id: str | None) -> str | None:
    """Extract ONT ID from external_id.

    Handles formats:
        - Simple: "5" -> "5"
        - SNMP-style: "huawei:4194304000.5" -> "5"
    """
    if not external_id:
        return None
    if "." in external_id:
        # SNMP-style: extract suffix after last dot
        return external_id.rsplit(".", 1)[-1]
    return external_id


def _normalize_board_port(board: str | None, port: str | None) -> str | None:
    """Normalize board/port to FSP format."""
    if not board or not port:
        return None
    # board is like "0/2", port is like "0" -> "0/2/0"
    return f"{board}/{port}"


def get_ont_by_fsp_and_id(
    db, olt_id, fsp: str, ont_id: int
) -> tuple[OntUnit | None, OntAssignment | None]:
    """Find ONT by FSP and ONT ID on the OLT."""
    # First try to find by simple external_id (ONT ID on OLT)
    stmt = select(OntUnit).where(
        OntUnit.olt_device_id == olt_id,
        OntUnit.external_id == str(ont_id),
        OntUnit.is_active.is_(True),
    )
    ont = db.scalars(stmt).first()
    if ont:
        # Get assignment if exists
        stmt = select(OntAssignment).where(
            OntAssignment.ont_unit_id == ont.id,
            OntAssignment.active.is_(True),
        )
        assignment = db.scalars(stmt).first()
        return ont, assignment

    # Try matching by board/port + extracted ONT ID from SNMP-style external_id
    stmt = select(OntUnit).where(
        OntUnit.olt_device_id == olt_id,
        OntUnit.is_active.is_(True),
    )
    for ont in db.scalars(stmt).all():
        ont_fsp = _normalize_board_port(ont.board, ont.port)
        extracted_id = _extract_ont_id_from_external(ont.external_id)
        if ont_fsp == fsp and extracted_id == str(ont_id):
            # Found match by FSP + ONT ID
            stmt = select(OntAssignment).where(
                OntAssignment.ont_unit_id == ont.id,
                OntAssignment.active.is_(True),
            )
            assignment = db.scalars(stmt).first()
            return ont, assignment

    return None, None


def import_from_config(
    config_dir: Path,
    dry_run: bool = False,
) -> dict[str, int]:
    """Import service-ports from config files."""
    results = {
        "created": 0,
        "skipped_existing": 0,
        "skipped_no_ont": 0,
        "skipped_no_olt": 0,
        "failed": 0,
    }

    db = SessionLocal()
    try:
        # Find all config files
        config_files = list(config_dir.glob("*.cfg"))
        logger.info("Found %d config files", len(config_files))

        for config_file in sorted(config_files):
            file_key = config_file.stem.lower()
            olt_pattern = OLT_NAME_MAPPING.get(file_key)

            if not olt_pattern:
                logger.warning("No OLT mapping for %s, skipping", config_file.name)
                continue

            # Find the OLT
            olt = find_olt_by_name_pattern(db, olt_pattern)
            if not olt:
                logger.warning("OLT not found for pattern '%s'", olt_pattern)
                results["skipped_no_olt"] += 1
                continue

            logger.info("Processing %s -> OLT %s", config_file.name, olt.name)

            # Parse service-ports from config
            service_ports = parse_config_file(config_file)
            logger.info("  Found %d service-port entries", len(service_ports))

            if not service_ports:
                continue

            # Get or create pool for this OLT
            stmt = select(OltServicePortPool).where(
                OltServicePortPool.olt_device_id == olt.id
            )
            pool = db.scalars(stmt).first()
            if not pool:
                pool = OltServicePortPool(
                    olt_device_id=olt.id,
                    min_index=0,
                    max_index=8191,
                )
                db.add(pool)
                db.flush()
                logger.info("  Created pool for OLT %s", olt.name)

            # Get existing allocations
            stmt = select(ServicePortAllocation.port_index).where(
                ServicePortAllocation.pool_id == pool.id
            )
            existing_indices = set(db.scalars(stmt).all())

            file_results = {
                "created": 0,
                "skipped_existing": 0,
                "skipped_no_ont": 0,
            }

            for sp in service_ports:
                # Skip if already exists
                if sp.index in existing_indices:
                    file_results["skipped_existing"] += 1
                    continue

                # Try to find the ONT
                ont, assignment = get_ont_by_fsp_and_id(db, olt.id, sp.fsp, sp.ont_id)

                if not ont:
                    logger.debug(
                        "    Skipping port %d: ONT %s/%d not found",
                        sp.index,
                        sp.fsp,
                        sp.ont_id,
                    )
                    file_results["skipped_no_ont"] += 1
                    continue

                # Create allocation
                if not dry_run:
                    allocation = ServicePortAllocation(
                        pool_id=pool.id,
                        port_index=sp.index,
                        ont_unit_id=ont.id,
                        vlan_id=sp.vlan_id,
                        gem_index=sp.gemport,
                        is_active=True,
                        provisioned_at=datetime.now(UTC),
                    )
                    db.add(allocation)
                    existing_indices.add(sp.index)

                file_results["created"] += 1
                logger.debug(
                    "    Created allocation: index=%d, ONT=%s, VLAN=%d",
                    sp.index,
                    ont.serial_number,
                    sp.vlan_id,
                )

            # No next_index tracking needed - allocations are tracked in table

            logger.info(
                "  %s: created=%d, skipped_existing=%d, skipped_no_ont=%d",
                config_file.name,
                file_results["created"],
                file_results["skipped_existing"],
                file_results["skipped_no_ont"],
            )

            results["created"] += file_results["created"]
            results["skipped_existing"] += file_results["skipped_existing"]
            results["skipped_no_ont"] += file_results["skipped_no_ont"]

        if not dry_run:
            db.commit()
            logger.info("Changes committed to database")
        else:
            logger.info("DRY RUN - no changes made")

    except Exception as e:
        logger.exception("Import failed: %s", e)
        db.rollback()
        raise
    finally:
        db.close()

    return results


def main():
    parser = argparse.ArgumentParser(
        description="Import service-ports from OLT config files"
    )
    parser.add_argument(
        "config_dir",
        type=Path,
        help="Directory containing OLT config files",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Parse and report but don't create records",
    )
    args = parser.parse_args()

    if not args.config_dir.is_dir():
        logger.error("Config directory not found: %s", args.config_dir)
        sys.exit(1)

    if args.dry_run:
        logger.info("DRY RUN - no changes will be made")

    results = import_from_config(args.config_dir, dry_run=args.dry_run)

    logger.info("=" * 60)
    logger.info("TOTAL RESULTS:")
    logger.info("  Created: %d", results["created"])
    logger.info("  Skipped (existing): %d", results["skipped_existing"])
    logger.info("  Skipped (no ONT): %d", results["skipped_no_ont"])
    logger.info("  Skipped (no OLT): %d", results["skipped_no_olt"])


if __name__ == "__main__":
    main()
