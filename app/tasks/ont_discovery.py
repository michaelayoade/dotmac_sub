"""Celery task for periodic ONT discovery via SNMP on all active OLTs."""

from __future__ import annotations

import logging

from sqlalchemy import select, text

from app.celery_app import celery_app
from app.db import SessionLocal
from app.models.network import OLTDevice

logger = logging.getLogger(__name__)
_ONT_DISCOVERY_LOCK_KEY = 70420612


@celery_app.task(name="app.tasks.ont_discovery.discover_all_olt_onts")
def discover_all_olt_onts() -> dict[str, int]:
    """Discover ONTs from all active OLTs via SNMP and upsert OntUnit rows.

    Iterates every active OLT, runs vendor-specific SNMP walks, and
    creates/updates OntUnit, PonPort, and OntAssignment records.

    Returns:
        Statistics dict with olts_scanned, onts_created, onts_updated, errors.
    """
    logger.info("Starting ONT discovery task")
    db = SessionLocal()
    lock_acquired = False
    try:
        lock_acquired = bool(
            db.execute(
                text("SELECT pg_try_advisory_lock(:key)"),
                {"key": _ONT_DISCOVERY_LOCK_KEY},
            ).scalar()
        )
        if not lock_acquired:
            logger.warning("Skipping ONT discovery: previous run still in progress.")
            return {
                "olts_scanned": 0,
                "onts_created": 0,
                "onts_updated": 0,
                "errors": 0,
                "skipped_due_to_lock": 1,
            }

        from app.services.web_network_olts import sync_onts_from_olt_snmp

        olts = list(
            db.scalars(
                select(OLTDevice).where(OLTDevice.is_active.is_(True))
            ).all()
        )
        logger.info("ONT discovery: found %d active OLTs", len(olts))

        olts_scanned = 0
        onts_created = 0
        onts_updated = 0
        errors = 0

        for olt in olts:
            try:
                ok, msg, stats = sync_onts_from_olt_snmp(db, str(olt.id))
                if ok:
                    olts_scanned += 1
                    created_raw = stats.get("created", 0)
                    updated_raw = stats.get("updated", 0)
                    onts_created += int(str(created_raw)) if created_raw else 0
                    onts_updated += int(str(updated_raw)) if updated_raw else 0
                    logger.info(
                        "ONT discovery OLT %s (%s): %s",
                        olt.name,
                        olt.mgmt_ip,
                        stats,
                    )
                else:
                    logger.warning(
                        "ONT discovery skipped OLT %s (%s): %s",
                        olt.name,
                        olt.mgmt_ip,
                        msg,
                    )
            except Exception as e:
                errors += 1
                logger.error(
                    "ONT discovery failed for OLT %s (%s): %s",
                    olt.name,
                    olt.mgmt_ip,
                    e,
                )

        result = {
            "olts_scanned": olts_scanned,
            "onts_created": onts_created,
            "onts_updated": onts_updated,
            "errors": errors,
        }
        logger.info("ONT discovery complete: %s", result)
        return result
    except Exception as e:
        logger.error("ONT discovery task failed: %s", e)
        db.rollback()
        raise
    finally:
        if lock_acquired:
            try:
                db.execute(
                    text("SELECT pg_advisory_unlock(:key)"),
                    {"key": _ONT_DISCOVERY_LOCK_KEY},
                )
            except Exception:
                logger.exception("Failed to release ONT discovery advisory lock.")
        db.close()
