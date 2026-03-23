"""Celery tasks for OLT optical signal polling."""

from __future__ import annotations

import logging

from sqlalchemy import text

from app.celery_app import celery_app
from app.db import SessionLocal

logger = logging.getLogger(__name__)
_OLT_POLLING_LOCK_KEY = 70420611


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
    lock_acquired = False
    try:
        lock_acquired = bool(
            db.execute(
                text("SELECT pg_try_advisory_lock(:key)"),
                {"key": _OLT_POLLING_LOCK_KEY},
            ).scalar()
        )
        if not lock_acquired:
            logger.warning(
                "Skipping OLT signal polling: previous run still in progress."
            )
            return {
                "olts_polled": 0,
                "total_polled": 0,
                "total_updated": 0,
                "total_errors": 0,
                "skipped_due_to_lock": 1,
            }

        from app.services.network.olt_polling import poll_all_olts

        result = poll_all_olts(db)
        logger.info(
            "OLT signal polling complete: %d OLTs, %d polled, %d updated, %d errors",
            result["olts_polled"],
            result["total_polled"],
            result["total_updated"],
            result["total_errors"],
        )

        # Push ONU status counts to VictoriaMetrics
        try:
            from app.services.monitoring_metrics import push_onu_status_metrics
            from app.services.network_monitoring import get_onu_status_summary

            onu = get_onu_status_summary(db)
            push_onu_status_metrics(
                online=onu.get("online", 0),
                offline=onu.get("offline", 0),
                low_signal=onu.get("low_signal", 0),
            )
        except Exception as exc:
            logger.warning("Failed to push ONU metrics to VictoriaMetrics: %s", exc)

        return result
    except Exception as e:
        logger.error("OLT signal polling task failed: %s", e, exc_info=True)
        db.rollback()
        raise
    finally:
        if lock_acquired:
            try:
                db.execute(
                    text("SELECT pg_advisory_unlock(:key)"),
                    {"key": _OLT_POLLING_LOCK_KEY},
                )
            except Exception:
                logger.exception("Failed to release OLT polling advisory lock.")
        db.close()
