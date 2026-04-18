#!/usr/bin/env python
"""Import existing service-ports from OLTs into DB allocator.

This script reads all service-ports from each OLT via SSH and creates
corresponding ServicePortAllocation records in the database.

Run after migration 035_add_provisioning_architecture.py to backfill
existing data.

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
from uuid import UUID

# Add app to path
sys.path.insert(0, "/opt/dotmac_sub")

from sqlalchemy import select

from app.db import SessionLocal
from app.models.network import (
    OLTDevice,
    OltServicePortPool,
    OntAssignment,
    OntUnit,
    PonPort,
    ServicePortAllocation,
)

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


def get_or_create_pool(db, olt: OLTDevice) -> OltServicePortPool:
    """Get or create service-port pool for OLT."""
    stmt = select(OltServicePortPool).where(
        OltServicePortPool.olt_device_id == olt.id,
        OltServicePortPool.is_active.is_(True),
    )
    pool = db.scalars(stmt).first()

    if not pool:
        pool = OltServicePortPool(
            olt_device_id=olt.id,
            min_index=0,
            max_index=65535,
        )
        db.add(pool)
        db.flush()
        logger.info("Created pool for OLT %s", olt.name)

    return pool


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
    stmt = select(PonPort).where(
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
        "created": 0,
        "skipped_existing": 0,
        "skipped_no_ont": 0,
        "failed": 0,
    }

    logger.info("Importing service-ports from OLT %s (%s)", olt.name, olt.mgmt_ip)

    # Get or create pool
    pool = get_or_create_pool(db, olt)

    # Get existing allocations for this pool
    stmt = select(ServicePortAllocation.port_index).where(
        ServicePortAllocation.pool_id == pool.id,
        ServicePortAllocation.is_active.is_(True),
    )
    existing_indices = set(db.scalars(stmt).all())

    # Get all PON ports for this OLT
    stmt = select(PonPort).where(PonPort.olt_id == olt.id)
    pon_ports = list(db.scalars(stmt).all())

    for pon_port in pon_ports:
        fsp = pon_port.name
        logger.info("  Reading service-ports from %s...", fsp)

        try:
            ok, msg, ports = get_service_ports(olt, fsp)
            if not ok:
                logger.warning("    Failed to read: %s", msg)
                results["failed"] += 1
                continue

            logger.info("    Found %d service-ports", len(ports))

            for port in ports:
                # Skip if already exists
                if port.index in existing_indices:
                    results["skipped_existing"] += 1
                    continue

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
                    results["skipped_no_ont"] += 1
                    continue

                # Create allocation
                if not dry_run:
                    allocation = ServicePortAllocation(
                        pool_id=pool.id,
                        ont_unit_id=ont.id,
                        port_index=port.index,
                        vlan_id=port.vlan_id,
                        gem_index=port.gem_index,
                        service_type=port.flow_type or "internet",
                        is_active=True,
                        provisioned_at=datetime.now(UTC),
                    )
                    db.add(allocation)
                    existing_indices.add(port.index)

                results["created"] += 1
                logger.debug(
                    "    Created allocation for port %d (ONT %s, VLAN %d)",
                    port.index,
                    ont.serial_number,
                    port.vlan_id,
                )

        except Exception as e:
            logger.error("    Error reading service-ports: %s", e)
            results["failed"] += 1

    # Update pool cache
    if not dry_run and results["created"] > 0:
        total_range = pool.max_index - pool.min_index + 1
        pool.available_count = total_range - len(existing_indices)
        # Find next available
        for idx in range(pool.min_index, pool.max_index + 1):
            if idx not in existing_indices:
                pool.next_available_index = idx
                break

        db.flush()

    return results


def main():
    parser = argparse.ArgumentParser(
        description="Import existing service-ports from OLTs into DB allocator"
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
            "created": 0,
            "skipped_existing": 0,
            "skipped_no_ont": 0,
            "failed": 0,
        }

        for olt in olts:
            try:
                results = import_service_ports_for_olt(db, olt, dry_run=args.dry_run)
                for key, value in results.items():
                    total_results[key] += value

                logger.info(
                    "OLT %s: created=%d, skipped_existing=%d, skipped_no_ont=%d, failed=%d",
                    olt.name,
                    results["created"],
                    results["skipped_existing"],
                    results["skipped_no_ont"],
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
        logger.info("  Created: %d", total_results["created"])
        logger.info("  Skipped (existing): %d", total_results["skipped_existing"])
        logger.info("  Skipped (no ONT): %d", total_results["skipped_no_ont"])
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
