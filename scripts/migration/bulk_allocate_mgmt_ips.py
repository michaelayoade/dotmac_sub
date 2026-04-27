#!/usr/bin/env python3
"""One-time migration: Allocate management IPs to all ONTs missing them.

This script:
1. Finds all active ONTs without a management IP in their assignment
2. Allocates an IP from the OLT's management IP pool
3. Updates the ont_assignments.mgmt_ip_address field

Usage:
    # Dry run (default) - show what would be changed
    python scripts/migration/bulk_allocate_mgmt_ips.py

    # Execute the migration
    python scripts/migration/bulk_allocate_mgmt_ips.py --execute
"""

from __future__ import annotations

import argparse
import logging
import sys
from collections import defaultdict

# Add the app directory to the path (works both locally and in container)
import os
app_dir = os.environ.get("APP_DIR", "/opt/dotmac_sub")
if not os.path.exists(app_dir):
    app_dir = "/app"  # Docker container path
sys.path.insert(0, app_dir)

from sqlalchemy import select
from sqlalchemy.orm import joinedload

from app.db import SessionLocal
from app.models.network import (
    MgmtIpMode,
    OLTDevice,
    OntAssignment,
    OntUnit,
    OnuOnlineStatus,
)
from app.services.network.ont_authorization import (
    _get_or_create_active_assignment,
    refresh_pool_availability,
)

logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")
logger = logging.getLogger(__name__)


def get_onts_needing_mgmt_ip(db) -> list[tuple[OntUnit, OLTDevice]]:
    """Find all ONTs without management IPs that have an OLT with a pool."""
    stmt = (
        select(OntUnit)
        .options(
            joinedload(OntUnit.olt_device),
            joinedload(OntUnit.assignments),
        )
        .join(OLTDevice, OntUnit.olt_device_id == OLTDevice.id)
        .where(
            OntUnit.is_active.is_(True),
            OLTDevice.mgmt_ip_pool_id.isnot(None),
        )
        .order_by(OLTDevice.name, OntUnit.serial_number)
    )

    results = []
    for ont in db.scalars(stmt).unique().all():
        # Check if assignment already has mgmt_ip
        assignment = None
        for a in getattr(ont, "assignments", []) or []:
            if getattr(a, "active", False):
                assignment = a
                break

        if assignment and assignment.mgmt_ip_address:
            continue  # Already has IP

        results.append((ont, ont.olt_device))

    return results


def allocate_mgmt_ips(db, execute: bool = False) -> dict:
    """Allocate management IPs to ONTs missing them."""
    from sqlalchemy import text

    onts_to_fix = get_onts_needing_mgmt_ip(db)

    stats = {
        "total_needing_ip": len(onts_to_fix),
        "allocated": 0,
        "skipped_no_pool": 0,
        "skipped_pool_exhausted": 0,
        "errors": 0,
        "by_olt": defaultdict(lambda: {"allocated": 0, "errors": 0}),
    }

    logger.info(f"Found {len(onts_to_fix)} ONTs needing management IPs")

    for ont, olt in onts_to_fix:
        olt_name = olt.name if olt else "Unknown"

        if not olt or not olt.mgmt_ip_pool_id:
            logger.debug(f"  {ont.serial_number}: No pool on OLT {olt_name}")
            stats["skipped_no_pool"] += 1
            continue

        try:
            # Get next available IP
            next_ip, available = refresh_pool_availability(db, olt.mgmt_ip_pool_id)
            if not next_ip:
                logger.warning(f"  {ont.serial_number}: Pool exhausted for {olt_name}")
                stats["skipped_pool_exhausted"] += 1
                continue

            if execute:
                # Get or create assignment
                assignment = _get_or_create_active_assignment(db, ont)

                # Reserve the IP using raw SQL (model has columns DB doesn't have)
                existing = db.execute(
                    text("SELECT id FROM ipv4_addresses WHERE address = :addr"),
                    {"addr": next_ip},
                ).fetchone()

                if existing is None:
                    db.execute(
                        text("""
                            INSERT INTO ipv4_addresses
                            (id, address, pool_id, is_reserved, notes, created_at, updated_at)
                            VALUES (gen_random_uuid(), :addr, :pool_id, true, :notes, now(), now())
                        """),
                        {
                            "addr": next_ip,
                            "pool_id": str(olt.mgmt_ip_pool_id),
                            "notes": f"ont:{ont.id}",
                        },
                    )
                else:
                    db.execute(
                        text("""
                            UPDATE ipv4_addresses
                            SET is_reserved = true, notes = :notes, updated_at = now()
                            WHERE address = :addr
                        """),
                        {"addr": next_ip, "notes": f"ont:{ont.id}"},
                    )

                # Update assignment
                assignment.mgmt_ip_address = next_ip
                assignment.mgmt_ip_mode = MgmtIpMode.static_ip

                # Also store in desired_config
                ont.mgmt_ip_address = next_ip

                db.flush()
                logger.info(f"  {ont.serial_number}: Allocated {next_ip} ({olt_name})")
            else:
                logger.info(
                    f"  {ont.serial_number}: Would allocate {next_ip} ({olt_name})"
                )

            stats["allocated"] += 1
            stats["by_olt"][olt_name]["allocated"] += 1

        except Exception as e:
            logger.error(f"  {ont.serial_number}: Error - {e}")
            stats["errors"] += 1
            stats["by_olt"][olt_name]["errors"] += 1

    return stats


def main():
    parser = argparse.ArgumentParser(
        description="Allocate management IPs to ONTs missing them"
    )
    parser.add_argument(
        "--execute",
        action="store_true",
        help="Actually perform the migration (default is dry-run)",
    )
    args = parser.parse_args()

    db = SessionLocal()
    try:
        logger.info("=" * 60)
        if args.execute:
            logger.info("EXECUTING migration - changes will be committed")
        else:
            logger.info("DRY RUN - no changes will be made")
        logger.info("=" * 60)

        stats = allocate_mgmt_ips(db, execute=args.execute)

        if args.execute:
            db.commit()
            logger.info("Changes committed successfully")
        else:
            db.rollback()
            logger.info("Dry run complete - no changes made")

        logger.info("")
        logger.info("=" * 60)
        logger.info("Summary:")
        logger.info(f"  Total ONTs needing IP: {stats['total_needing_ip']}")
        logger.info(f"  Would allocate/Allocated: {stats['allocated']}")
        logger.info(f"  Skipped (no pool): {stats['skipped_no_pool']}")
        logger.info(f"  Skipped (pool exhausted): {stats['skipped_pool_exhausted']}")
        logger.info(f"  Errors: {stats['errors']}")
        logger.info("")
        logger.info("By OLT:")
        for olt_name, olt_stats in sorted(stats["by_olt"].items()):
            logger.info(
                f"  {olt_name}: {olt_stats['allocated']} allocated, "
                f"{olt_stats['errors']} errors"
            )
        logger.info("=" * 60)

    except Exception as e:
        db.rollback()
        logger.error(f"Migration failed: {e}")
        raise
    finally:
        db.close()


if __name__ == "__main__":
    main()
