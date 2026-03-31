"""Celery task for periodic OLT hardware inventory discovery via SNMP."""

from __future__ import annotations

import logging

from sqlalchemy import select, text

from app.celery_app import celery_app
from app.db import task_session
from app.models.network import OLTDevice

logger = logging.getLogger(__name__)
_HW_DISCOVERY_LOCK_KEY = 70420613


@celery_app.task(name="app.tasks.olt_hardware_discovery.discover_all_olt_hardware")
def discover_all_olt_hardware() -> dict[str, int]:
    """Discover hardware inventory from all active OLTs via SNMP Entity MIB.

    Iterates every active OLT with SNMP enabled, walks the Entity MIB,
    and upserts shelf, card, port, power unit, and fan unit records.

    Returns:
        Statistics dict with olts_scanned, created, updated, errors.
    """
    logger.info("Starting OLT hardware discovery task")
    with task_session() as db:
        lock_acquired = bool(
            db.execute(
                text("SELECT pg_try_advisory_lock(:key)"),
                {"key": _HW_DISCOVERY_LOCK_KEY},
            ).scalar()
        )
        if not lock_acquired:
            logger.warning(
                "Skipping OLT hardware discovery: previous run still in progress."
            )
            return {
                "olts_scanned": 0,
                "created": 0,
                "updated": 0,
                "errors": 0,
                "skipped_due_to_lock": 1,
            }

        try:
            from app.services.network.olt_hardware_discovery import (
                discover_olt_hardware,
            )

            olts = list(
                db.scalars(
                    select(OLTDevice).where(
                        OLTDevice.is_active.is_(True),
                        OLTDevice.snmp_ro_community.isnot(None),
                        OLTDevice.snmp_ro_community != "",
                    )
                ).all()
            )
            logger.info(
                "OLT hardware discovery: found %d OLTs with SNMP credentials",
                len(olts),
            )

            olts_scanned = 0
            total_created = 0
            total_updated = 0
            errors = 0

            for olt in olts:
                try:
                    ok, msg, olt_stats = discover_olt_hardware(db, olt)
                    if ok:
                        olts_scanned += 1
                        total_created += sum(
                            int(str(v))
                            for k, v in olt_stats.items()
                            if k.endswith("_created") and isinstance(v, int)
                        )
                        total_updated += sum(
                            int(str(v))
                            for k, v in olt_stats.items()
                            if k.endswith("_updated") and isinstance(v, int)
                        )
                        logger.info(
                            "Hardware discovery OLT %s (%s): %s — %s",
                            olt.name,
                            olt.mgmt_ip,
                            msg,
                            olt_stats,
                        )
                    else:
                        logger.warning(
                            "Hardware discovery skipped OLT %s (%s): %s",
                            olt.name,
                            olt.mgmt_ip,
                            msg,
                        )
                except Exception as e:
                    errors += 1
                    logger.error(
                        "Hardware discovery failed for OLT %s (%s): %s",
                        olt.name,
                        olt.mgmt_ip,
                        e,
                    )

            result = {
                "olts_scanned": olts_scanned,
                "created": total_created,
                "updated": total_updated,
                "errors": errors,
            }
            logger.info("OLT hardware discovery complete: %s", result)
            return result
        finally:
            try:
                db.execute(
                    text("SELECT pg_advisory_unlock(:key)"),
                    {"key": _HW_DISCOVERY_LOCK_KEY},
                )
            except Exception:
                logger.exception(
                    "Failed to release OLT hardware discovery advisory lock."
                )
