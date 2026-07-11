"""Shared observability recording helpers.

This is a thin policy boundary over existing sinks: Prometheus metrics,
Redis-backed task/job heartbeats, and admin alerts. It gives domain code one
place to record operational signals without each task reimplementing routing.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from sqlalchemy.orm import Session

from app.models.network_monitoring import AlertSeverity
from app.services import admin_alerts, job_heartbeat, task_heartbeat

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class Finding:
    fingerprint: str
    domain: str
    source: str
    severity: AlertSeverity
    title: str
    summary: str
    details: dict[str, Any] = field(default_factory=dict)
    target_url: str = "/admin/system/health"


def record_task_run(
    task_name: str,
    *,
    status: str,
    counters: dict[str, Any] | None = None,
    duration_seconds: float | None = None,
    now: datetime | None = None,
) -> None:
    """Record a task run across the existing heartbeat/metrics sinks."""
    if not task_name:
        return
    normalized_status = "ok" if status in {"ok", "success"} else status
    metric_status = status or normalized_status
    counters = counters if isinstance(counters, dict) else {}
    try:
        if normalized_status == "ok":
            job_heartbeat.record_success(task_name, now=now)
            task_heartbeat.record_success(task_name, counters, now=now)
        job_heartbeat.record_result(
            task_name,
            status=normalized_status,
            detail=counters,
            now=now,
        )
    except Exception:
        logger.exception("observability_task_run_record_failed task=%s", task_name)

    try:
        from app.metrics import OBSERVABILITY_EVENTS_TOTAL, observe_job

        OBSERVABILITY_EVENTS_TOTAL.labels(
            domain="task",
            signal=task_name,
            status=normalized_status,
        ).inc()
        if duration_seconds is not None:
            observe_job(task_name, metric_status, duration_seconds)
    except Exception:
        logger.debug(
            "observability_task_metrics_record_failed task=%s",
            task_name,
            exc_info=True,
        )


def record_task_skip(
    task_name: str,
    *,
    reason: str = "skipped",
    now: datetime | None = None,
) -> int:
    """Record a task skip and return the consecutive skip streak."""
    if not task_name:
        return 0
    streak = 0
    detail: dict[str, Any] = {"reason": reason}
    try:
        streak = task_heartbeat.record_skip(task_name)
        detail["skip_streak"] = streak
        job_heartbeat.record_result(
            task_name,
            status="skipped",
            detail=detail,
            now=now,
        )
    except Exception:
        logger.exception("observability_task_skip_record_failed task=%s", task_name)

    try:
        from app.metrics import OBSERVABILITY_EVENTS_TOTAL

        OBSERVABILITY_EVENTS_TOTAL.labels(
            domain="task",
            signal=task_name,
            status="skipped",
        ).inc()
    except Exception:
        logger.debug(
            "observability_task_skip_metrics_record_failed task=%s",
            task_name,
            exc_info=True,
        )
    return streak


def record_celery_task_success(
    task_name: str,
    *,
    result: Any = None,
    now: datetime | None = None,
) -> None:
    """Record framework-level Celery task success signals."""
    if not task_name:
        return
    detail = result if isinstance(result, dict) else None
    try:
        job_heartbeat.record_success(task_name, now=now)
        if task_name in job_heartbeat.MONEY_JOB_TASKS:
            job_heartbeat.record_result(
                task_name,
                status="ok",
                detail=detail,
                now=now,
            )
    except Exception:
        logger.debug(
            "observability_celery_success_record_failed task=%s",
            task_name,
            exc_info=True,
        )

    try:
        from app.metrics import OBSERVABILITY_EVENTS_TOTAL

        OBSERVABILITY_EVENTS_TOTAL.labels(
            domain="celery",
            signal=task_name,
            status="success",
        ).inc()
    except Exception:
        logger.debug(
            "observability_celery_success_metrics_failed task=%s",
            task_name,
            exc_info=True,
        )


def record_celery_task_failure(
    task_name: str,
    *,
    error: str | None = None,
    now: datetime | None = None,
) -> None:
    """Record framework-level Celery task failure signals."""
    if not task_name:
        return
    try:
        if task_name in job_heartbeat.MONEY_JOB_TASKS:
            msg = error if error is not None else "unknown error"
            job_heartbeat.record_result(
                task_name,
                status="error",
                detail={"error": msg[:500]},
                now=now,
            )
    except Exception:
        logger.debug(
            "observability_celery_failure_record_failed task=%s",
            task_name,
            exc_info=True,
        )

    try:
        from app.metrics import OBSERVABILITY_EVENTS_TOTAL

        OBSERVABILITY_EVENTS_TOTAL.labels(
            domain="celery",
            signal=task_name,
            status="error",
        ).inc()
    except Exception:
        logger.debug(
            "observability_celery_failure_metrics_failed task=%s",
            task_name,
            exc_info=True,
        )


def record_metric(
    *,
    domain: str,
    signal: str,
    status: str = "observed",
    count: int = 1,
) -> None:
    """Record a lightweight Prometheus counter signal."""
    if count <= 0:
        return
    try:
        from app.metrics import OBSERVABILITY_EVENTS_TOTAL

        OBSERVABILITY_EVENTS_TOTAL.labels(
            domain=domain,
            signal=signal,
            status=status,
        ).inc(count)
    except Exception:
        logger.debug(
            "observability_metric_record_failed domain=%s signal=%s",
            domain,
            signal,
            exc_info=True,
        )


def record_finding(db: Session, finding: Finding) -> str:
    """Sync a single operational finding into the admin alert lifecycle."""
    return admin_alerts.sync_alert(
        db,
        admin_alerts.AlertFinding(
            fingerprint=finding.fingerprint,
            category=finding.domain,
            source=finding.source,
            severity=finding.severity,
            title=finding.title,
            summary=finding.summary,
            details=finding.details,
            target_url=finding.target_url,
        ),
    )


def resolve_findings(
    db: Session,
    *,
    managed_prefix: str,
    active_fingerprints: set[str],
) -> int:
    """Resolve admin alerts under a managed prefix that are no longer active."""
    return admin_alerts.resolve_missing_alerts(
        db,
        managed_prefix=managed_prefix,
        active_fingerprints=active_fingerprints,
    )


def record_notification_queue_result(
    db: Session,
    *,
    task_name: str,
    result: dict[str, int],
    duration_seconds: float | None = None,
) -> None:
    """Record notification delivery batch counters and failure findings."""
    counters = {key: int(value or 0) for key, value in result.items()}
    record_task_run(
        task_name,
        status="ok",
        counters=counters,
        duration_seconds=duration_seconds,
        now=datetime.now(UTC),
    )
    try:
        from app.metrics import NOTIFICATION_QUEUE_OUTCOMES_TOTAL

        for outcome, count in counters.items():
            if count > 0:
                NOTIFICATION_QUEUE_OUTCOMES_TOTAL.labels(outcome=outcome).inc(count)
    except Exception:
        logger.debug("notification_queue_metrics_record_failed", exc_info=True)

    failed = counters.get("failed", 0)
    stuck_dropped = counters.get("stuck_dropped", 0)
    if failed <= 0 and stuck_dropped <= 0:
        return

    severity = AlertSeverity.critical if failed >= 10 else AlertSeverity.warning
    try:
        record_finding(
            db,
            Finding(
                fingerprint="observability:notification:queue-failures",
                domain="notification",
                source="notification_queue",
                severity=severity,
                title="Notification queue delivery failures",
                summary=(
                    f"{failed} notification(s) failed; "
                    f"{stuck_dropped} stuck send(s) dropped in the latest batch."
                ),
                details=counters,
                target_url="/admin/notifications",
            ),
        )
    except Exception:
        logger.exception("notification_queue_finding_record_failed")
