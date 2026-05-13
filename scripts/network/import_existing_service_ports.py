#!/usr/bin/env python
"""Import existing service-ports from OLTs into observed OLT state.

This script reads all service-ports from each OLT via SSH and creates
corresponding OltServicePort records in the database.

Run after migration 096_add_imported_olt_service_ports.py to backfill observed
state.

Usage:
    poetry run python scripts/import_existing_service_ports.py
    poetry run python scripts/import_existing_service_ports.py --dry-run
    poetry run python scripts/import_existing_service_ports.py --olt-id <uuid>
"""

from __future__ import annotations

import argparse
import logging
import sys
from datetime import UTC, datetime
from typing import Any
from uuid import UUID

# Add app to path
sys.path.insert(0, "/opt/dotmac_sub")

from sqlalchemy import select

from app.db import SessionLocal
from app.models.network import (
    OLTDevice,
    OltServicePort,
    OntAssignment,
    OntUnit,
    PonPort,
)
from app.services.network.olt_ssh_ont._common import normalize_fsp

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
logger = logging.getLogger(__name__)


def get_all_active_olts(db) -> list[OLTDevice]:
    """Get all active OLTs."""
    stmt = select(OLTDevice).where(
        OLTDevice.is_active.is_(True),
        OLTDevice.mgmt_ip.isnot(None),
    )
    return list(db.scalars(stmt).all())


def get_ont_by_external_id(db, olt_id: UUID, external_id: str) -> OntUnit | None:
    """Find ONT by OLT and external_id (ONT-ID on OLT)."""
    stmt = select(OntUnit).where(
        OntUnit.olt_device_id == olt_id,
        OntUnit.external_id == external_id,
        OntUnit.is_active.is_(True),
    )
    return db.scalars(stmt).first()


def get_ont_by_pon_port_and_id(
    db, olt_id: UUID, fsp: str, ont_id: int
) -> OntUnit | None:
    """Find ONT by PON port and ONT-ID."""
    # Find PON port
    stmt: Any = select(PonPort).where(
        PonPort.olt_id == olt_id,
        PonPort.name == fsp,
    )
    pon_port = db.scalars(stmt).first()
    if not pon_port:
        return None

    # Find assignment on this port
    stmt = select(OntAssignment).where(
        OntAssignment.pon_port_id == pon_port.id,
        OntAssignment.active.is_(True),
    )
    assignments = list(db.scalars(stmt).all())

    # Match by external_id
    for assignment in assignments:
        ont = db.get(OntUnit, assignment.ont_unit_id)
        if ont and ont.external_id == str(ont_id):
            return ont

    return None


def import_service_ports_for_olt(
    db,
    olt: OLTDevice,
    *,
    dry_run: bool = False,
) -> dict[str, int]:
    """Import service ports for a single OLT."""
    from app.services.network.olt_ssh import get_service_ports

    results = {
        "imported": 0,
        "updated": 0,
        "unmatched_ont": 0,
        "failed": 0,
    }

    logger.info("Importing service-ports from OLT %s (%s)", olt.name, olt.mgmt_ip)

    existing_ports = {
        port.port_index: port
        for port in db.scalars(
            select(OltServicePort).where(OltServicePort.olt_device_id == olt.id)
        ).all()
    }

    # Get all PON ports for this OLT
    pon_port_stmt = select(PonPort).where(PonPort.olt_id == olt.id)
    pon_ports = list(db.scalars(pon_port_stmt).all())

    for pon_port in pon_ports:
        fsp_raw = pon_port.name
        fsp = normalize_fsp(fsp_raw)
        logger.info("  Reading service-ports from %s...", fsp_raw)

        try:
            ok, msg, ports = get_service_ports(olt, fsp)
            if not ok:
                logger.warning("    Failed to read: %s", msg)
                results["failed"] += 1
                continue

            logger.info("    Found %d service-ports", len(ports))

            for port in ports:
                # Try to find the ONT
                ont = get_ont_by_pon_port_and_id(db, olt.id, fsp, port.ont_id)
                if not ont:
                    ont = get_ont_by_external_id(db, olt.id, str(port.ont_id))

                if not ont:
                    logger.debug(
                        "    Skipping port %d: ONT %d not found in DB",
                        port.index,
                        port.ont_id,
                    )
                    results["unmatched_ont"] += 1

                if not dry_run:
                    existing = existing_ports.get(port.index)
                    values = {
                        "ont_unit_id": ont.id if ont else None,
                        "fsp": fsp,
                        "ont_id_on_olt": port.ont_id,
                        "vlan_id": port.vlan_id,
                        "gem_index": port.gem_index,
                        "user_vlan": getattr(port, "user_vlan", None),
                        "tag_transform": getattr(port, "tag_transform", None),
                        "flow_type": port.flow_type,
                        "flow_para": port.flow_para,
                        "state": port.state,
                        "source": "ssh",
                        "raw_entry": {
                            "service_port": port.index,
                            "vlan": port.vlan_id,
                            "fsp": fsp,
                            "ont_id": port.ont_id,
                            "gemport": port.gem_index,
                            "flow_type": port.flow_type,
                            "flow_para": port.flow_para,
                            "state": port.state,
                        },
                        "last_imported_at": datetime.now(UTC),
                    }
                    if existing is None:
                        existing = OltServicePort(
                            olt_device_id=olt.id,
                            port_index=port.index,
                            **values,
                        )
                        db.add(existing)
                        existing_ports[port.index] = existing
                        results["imported"] += 1
                    else:
                        for key, value in values.items():
                            setattr(existing, key, value)
                        results["updated"] += 1
                else:
                    results["imported"] += 1

                logger.debug(
                    "    Imported service-port %d (ONT %s, VLAN %d)",
                    port.index,
                    getattr(ont, "serial_number", None) or "unmatched",
                    port.vlan_id,
                )

        except Exception as e:
            logger.error("    Error reading service-ports: %s", e)
            results["failed"] += 1

    if not dry_run and (results["imported"] > 0 or results["updated"] > 0):
        db.flush()

    return results


def main():
    parser = argparse.ArgumentParser(
        description="Import existing service-ports from OLTs into observed OLT state"
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Show what would be imported without making changes",
    )
    parser.add_argument(
        "--olt-id",
        type=str,
        help="Import only from specific OLT (by UUID)",
    )
    args = parser.parse_args()

    db = SessionLocal()
    try:
        if args.olt_id:
            olt = db.get(OLTDevice, args.olt_id)
            if not olt:
                logger.error("OLT not found: %s", args.olt_id)
                return 1
            olts = [olt]
        else:
            olts = get_all_active_olts(db)

        logger.info("Found %d active OLT(s) to process", len(olts))

        if args.dry_run:
            logger.info("DRY RUN - no changes will be made")

        total_results = {
            "imported": 0,
            "updated": 0,
            "unmatched_ont": 0,
            "failed": 0,
        }

        for olt in olts:
            try:
                results = import_service_ports_for_olt(db, olt, dry_run=args.dry_run)
                for key, value in results.items():
                    total_results[key] += value

                logger.info(
                    "OLT %s: imported=%d, updated=%d, unmatched_ont=%d, failed=%d",
                    olt.name,
                    results["imported"],
                    results["updated"],
                    results["unmatched_ont"],
                    results["failed"],
                )

            except Exception as e:
                logger.error("Error processing OLT %s: %s", olt.name, e)
                total_results["failed"] += 1

        if not args.dry_run:
            db.commit()
            logger.info("Changes committed to database")

        logger.info("=" * 60)
        logger.info("TOTAL RESULTS:")
        logger.info("  Imported: %d", total_results["imported"])
        logger.info("  Updated: %d", total_results["updated"])
        logger.info("  Unmatched ONT: %d", total_results["unmatched_ont"])
        logger.info("  Failed: %d", total_results["failed"])

        return 0

    except Exception as e:
        logger.exception("Fatal error: %s", e)
        db.rollback()
        return 1
    finally:
        db.close()


if __name__ == "__main__":
    sys.exit(main())
