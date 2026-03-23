"""Celery task for periodic aggregated OLT autofind discovery."""

from __future__ import annotations

import logging

from sqlalchemy import select, text

from app.celery_app import celery_app
from app.db import SessionLocal
from app.models.network import OLTDevice
from app.services import web_network_ont_autofind as ont_autofind_service

logger = logging.getLogger(__name__)
_ONT_AUTOFIND_LOCK_KEY = 70420613


@celery_app.task(name="app.tasks.ont_autofind.discover_all_olt_autofind")
def discover_all_olt_autofind() -> dict[str, int]:
    """Scan all active OLTs for unconfigured ONTs and cache the results."""
    logger.info("Starting aggregated OLT autofind task")
    db = SessionLocal()
    lock_acquired = False
    try:
        lock_acquired = bool(
            db.execute(
                text("SELECT pg_try_advisory_lock(:key)"),
                {"key": _ONT_AUTOFIND_LOCK_KEY},
            ).scalar()
        )
        if not lock_acquired:
            logger.warning("Skipping OLT autofind discovery: previous run still in progress.")
            return {
                "olts_scanned": 0,
                "candidates_created": 0,
                "candidates_updated": 0,
                "candidates_resolved": 0,
                "errors": 0,
                "skipped_due_to_lock": 1,
            }

        olts = list(
            db.scalars(select(OLTDevice).where(OLTDevice.is_active.is_(True))).all()
        )
        scanned = 0
        created = 0
        updated = 0
        resolved = 0
        errors = 0

        for olt in olts:
            try:
                ok, message, stats = ont_autofind_service.sync_olt_autofind_candidates(
                    db, str(olt.id)
                )
                if ok:
                    scanned += 1
                    created += int(stats.get("created", 0))
                    updated += int(stats.get("updated", 0))
                    resolved += int(stats.get("resolved", 0))
                    logger.info(
                        "OLT autofind cached for %s (%s): %s",
                        olt.name,
                        olt.mgmt_ip,
                        stats,
                    )
                else:
                    errors += 1
                    logger.warning(
                        "OLT autofind failed for %s (%s): %s",
                        olt.name,
                        olt.mgmt_ip,
                        message,
                    )
            except Exception as exc:
                errors += 1
                logger.error(
                    "OLT autofind task failed for %s (%s): %s",
                    olt.name,
                    olt.mgmt_ip,
                    exc,
                )

        result = {
            "olts_scanned": scanned,
            "candidates_created": created,
            "candidates_updated": updated,
            "candidates_resolved": resolved,
            "errors": errors,
        }
        logger.info("Aggregated OLT autofind complete: %s", result)
        return result
    finally:
        if lock_acquired:
            try:
                db.execute(
                    text("SELECT pg_advisory_unlock(:key)"),
                    {"key": _ONT_AUTOFIND_LOCK_KEY},
                )
            except Exception:
                logger.exception("Failed to release OLT autofind advisory lock.")
        db.close()
