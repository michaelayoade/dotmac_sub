"""Scheduled Huawei OLT MAC-forwarding harvest (hop-1 foundation).

Walks the active PON ports of every Huawei OLT, parses ``display mac-address
port <F/S/P>``, and upserts ForwardingObservation rows mapping each learned MAC
to its ONT/PON position, then runs a read-only ONT<->subscriber drift check.

Routed to the ``ingestion`` queue like the other topology/ingest tasks.
Read-only against the OLTs; single-flight via a Postgres advisory lock; commits
the observation upserts + prune on success.
"""

from __future__ import annotations

import logging
from typing import Any

from billiard.exceptions import SoftTimeLimitExceeded
from sqlalchemy import text

from app.celery_app import celery_app
from app.services.db_session_adapter import db_session_adapter

logger = logging.getLogger(__name__)

# Stable advisory-lock key (single-flight across workers/beats).
_HARVEST_LOCK_KEY = 70420614


@celery_app.task(
    name="app.tasks.olt_mac_harvest.run_olt_mac_harvest",
    soft_time_limit=600,
    time_limit=660,
)
def run_olt_mac_harvest() -> dict[str, Any]:
    """Harvest MAC-forwarding tables from all active Huawei OLTs."""
    db = db_session_adapter.create_session()
    try:
        lock_acquired = bool(
            db.execute(
                text("SELECT pg_try_advisory_lock(:key)"),
                {"key": _HARVEST_LOCK_KEY},
            ).scalar()
        )
        if not lock_acquired:
            logger.warning(
                "olt_mac_harvest_skip_locked: previous run still in progress."
            )
            return {"skipped_due_to_lock": 1}

        try:
            from app.services.topology.olt_mac_harvest import harvest_olt_mac_tables

            result = harvest_olt_mac_tables(db)
            db.commit()
            logger.info("olt_mac_harvest_task_done %s", result)
            return result
        except SoftTimeLimitExceeded:
            db.rollback()
            logger.warning("olt_mac_harvest_timed_out")
            return {"error": "olt_mac_harvest_timed_out"}
        except Exception as exc:  # noqa: BLE001 - report and roll back
            db.rollback()
            logger.exception("olt_mac_harvest_failed")
            return {"error": str(exc)}
        finally:
            try:
                db.execute(
                    text("SELECT pg_advisory_unlock(:key)"),
                    {"key": _HARVEST_LOCK_KEY},
                )
            except Exception:
                logger.exception("olt_mac_harvest_unlock_failed")
    finally:
        db.close()
