"""Scheduled trigger for customer outage notifications (ADR 0004).

The task is safe to schedule before the decision to enable automation is made:
the service is gated on ``outage_auto_notify_enabled`` (default off) and a
disabled run is a no-op. Single-flight via the same advisory-lock helper the
other topology sweeps use — two concurrent runs would each read the debounce
table before the other wrote it, and double-notify.
"""

from __future__ import annotations

import logging
from typing import Any

from billiard.exceptions import SoftTimeLimitExceeded

from app.celery_app import celery_app
from app.services.db_session_adapter import db_session_adapter

logger = logging.getLogger(__name__)

_LOCK_TIMEOUT_MS = 30_000


@celery_app.task(
    name="app.tasks.outage_auto_notify.auto_dispatch_outage_notifications",
    soft_time_limit=150,
    time_limit=180,
)
def auto_dispatch_outage_notifications() -> dict[str, Any]:
    """Notify customers about settled, high-confidence node outages."""
    from app.services.topology.outage_auto_notify import (
        ADVISORY_LOCK_KEY,
        auto_dispatch_due_outage_notifications,
    )

    with db_session_adapter.advisory_lock(
        ADVISORY_LOCK_KEY, timeout_ms=_LOCK_TIMEOUT_MS
    ) as (db, acquired):
        if not acquired:
            logger.info("outage_auto_notify_skipped: previous run still in progress")
            return {"skipped": "already_running"}
        try:
            result = auto_dispatch_due_outage_notifications(db)
            db.commit()
            return result
        except SoftTimeLimitExceeded:
            db.rollback()
            logger.warning("outage_auto_notify_timed_out")
            return {"error": "outage_auto_notify_timed_out"}
        except Exception as exc:  # noqa: BLE001 - report and roll back
            db.rollback()
            logger.exception("outage_auto_notify_failed")
            return {"error": str(exc)}
