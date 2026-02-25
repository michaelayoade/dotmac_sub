"""Celery tasks for OLT optical signal polling."""

from __future__ import annotations

import logging

from app.celery_app import celery_app
from app.db import SessionLocal

logger = logging.getLogger(__name__)


@celery_app.task(name="app.tasks.olt_polling.poll_all_olt_signals")
def poll_all_olt_signals() -> dict[str, int]:
    """Periodic task to poll all active OLTs for ONT signal levels.

    Walks SNMP OID tables on each OLT to collect per-ONT optical
    signal levels, online status, and distance estimates.

    Returns:
        Statistics dict with olts_polled, total_polled, total_updated, total_errors.
    """
    logger.info("Starting OLT signal polling task")
    db = SessionLocal()
    try:
        from app.services.network.olt_polling import poll_all_olts

        result = poll_all_olts(db)
        logger.info(
            "OLT signal polling complete: %d OLTs, %d polled, %d updated, %d errors",
            result["olts_polled"],
            result["total_polled"],
            result["total_updated"],
            result["total_errors"],
        )
        return result
    except Exception as e:
        logger.error("OLT signal polling task failed: %s", e)
        db.rollback()
        raise
    finally:
        db.close()
