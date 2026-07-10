"""Scheduled RADIUS health check (operations strategy priority 2).

Collects accounting-plane and enforcement-drift signals from the external
radacct DB + the reconciled live-session view, pushes trend series to
VictoriaMetrics, and records a task heartbeat the admin-alert evaluator
reads. Single-flight via the pinned advisory-lock helper; ingestion queue.
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
    name="app.tasks.radius_health.run_radius_health_check",
    soft_time_limit=90,
    time_limit=120,
)
def run_radius_health_check() -> dict[str, Any]:
    """Run one RADIUS health pass; push metrics and stamp the heartbeat."""
    from app.services.radius_health import (
        ADVISORY_LOCK_KEY,
        DEFAULT_STALE_SESSION_SECONDS,
        HEARTBEAT_TASK,
        collect_radius_health,
        push_radius_metrics,
    )
    from app.services.task_heartbeat import record_skip, record_success

    with db_session_adapter.advisory_lock(
        ADVISORY_LOCK_KEY, timeout_ms=_LOCK_TIMEOUT_MS
    ) as (db, acquired):
        if not acquired:
            streak = record_skip(HEARTBEAT_TASK)
            logger.info(
                "radius_health_skipped: previous run still in progress (streak=%d)",
                streak,
            )
            return {"skipped": "already_running", "skip_streak": streak}
        try:
            from app.models.domain_settings import SettingDomain
            from app.services.settings_spec import resolve_value

            try:
                stale_after = int(
                    resolve_value(
                        db,
                        SettingDomain.network_monitoring,
                        "radius_stale_session_seconds",
                    )
                    or DEFAULT_STALE_SESSION_SECONDS
                )
            except (TypeError, ValueError):
                stale_after = DEFAULT_STALE_SESSION_SECONDS

            health = collect_radius_health(
                db, stale_after_seconds=max(120, stale_after)
            )
            db.rollback()  # read-only pass; release snapshots promptly
            health.update(push_radius_metrics(health))
            record_success(HEARTBEAT_TASK, health)
            return health
        except SoftTimeLimitExceeded:
            db.rollback()
            logger.warning("radius_health_timed_out")
            return {"error": "radius_health_timed_out"}
        except Exception as exc:  # noqa: BLE001 - report and roll back
            db.rollback()
            logger.exception("radius_health_failed")
            return {"error": str(exc)}
