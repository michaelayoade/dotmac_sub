"""Scheduled customer-impact metrics (Customer Service SLA counters)."""

from __future__ import annotations

import logging
from typing import Any

from billiard.exceptions import SoftTimeLimitExceeded

from app.celery_app import celery_app
from app.services.db_session_adapter import db_session_adapter

logger = logging.getLogger(__name__)

_LOCK_TIMEOUT_MS = 30_000


@celery_app.task(
    name="app.tasks.customer_impact_metrics.export_customer_impact_metrics",
    soft_time_limit=120,
    time_limit=150,
)
def export_customer_impact_metrics() -> dict[str, Any]:
    """Compute fleet impact counters and push them to VictoriaMetrics."""
    from app.services.customer_impact_metrics import (
        ADVISORY_LOCK_KEY,
        HEARTBEAT_TASK,
        collect_customer_impact,
        push_customer_impact_metrics,
    )
    from app.services.observability import record_task_run, record_task_skip

    with db_session_adapter.advisory_lock(
        ADVISORY_LOCK_KEY, timeout_ms=_LOCK_TIMEOUT_MS
    ) as (db, acquired):
        if not acquired:
            streak = record_task_skip(HEARTBEAT_TASK, reason="already_running")
            logger.info(
                "customer_impact_metrics_skipped: previous run still in progress "
                "(streak=%d)",
                streak,
            )
            return {"skipped": "already_running", "skip_streak": streak}
        try:
            impact = collect_customer_impact(db)
            db.rollback()  # read-only pass
            impact.update(push_customer_impact_metrics(impact))
            record_task_run(HEARTBEAT_TASK, status="success", counters=impact)
            return impact
        except SoftTimeLimitExceeded:
            db.rollback()
            logger.warning("customer_impact_metrics_timed_out")
            record_task_run(
                HEARTBEAT_TASK,
                status="error",
                counters={"error": "customer_impact_metrics_timed_out"},
            )
            return {"error": "customer_impact_metrics_timed_out"}
        except Exception as exc:  # noqa: BLE001 - report and roll back
            db.rollback()
            logger.exception("customer_impact_metrics_failed")
            record_task_run(
                HEARTBEAT_TASK,
                status="error",
                counters={"error": str(exc)},
            )
            return {"error": str(exc)}
