"""Per-task last-success heartbeats (Redis-backed).

``ScheduledTask.last_run_at`` is NOT maintained by the celery beat loop (beat
keeps its own schedule state), so it cannot tell us whether a runner is alive.
Instead we record a heartbeat from the ``task_postrun`` signal on SUCCESS and
read it back in the billing-health monitor to detect stalled/dead runners — the
"billing queue has no consumer" class of outage.

Same cached-client, never-raise pattern as radius_reconciliation's audit store.
"""

from __future__ import annotations

import json
import logging
import os
from datetime import UTC, datetime
from typing import Any

logger = logging.getLogger(__name__)

_KEY_PREFIX = "job:heartbeat:success:"
_RESULT_KEY_PREFIX = "job:heartbeat:result:"
_TTL_SECONDS = int(30 * 24 * 3600)  # 30 days; freshness is judged by age, not TTL
_redis_client: Any = None

# Scheduled money jobs whose LAST-RUN result (status + returned counts) we surface
# in billing-health. Only these are instrumented — not every celery task.
MONEY_JOB_TASKS = (
    "app.tasks.billing.run_invoice_cycle",
    "app.tasks.billing.run_billing_notifications",
    "app.tasks.collections.run_billing_enforcement",
    "app.tasks.collections.run_bundle_reconcile",
    "app.tasks.collections.run_dunning",
)


def _get_redis() -> Any:
    global _redis_client
    if _redis_client is None:
        url = os.getenv("REDIS_URL")
        if not url:
            return None
        import redis

        # Cached per process — never build clients per call (OOM lesson).
        _redis_client = redis.Redis.from_url(
            url, socket_timeout=2, socket_connect_timeout=2
        )
    return _redis_client


def record_success(task_name: str, *, now: datetime | None = None) -> bool:
    """Stamp the last successful completion time for a task. Never raises."""
    if not task_name:
        return False
    client = _get_redis()
    if client is None:
        return False
    try:
        ts = (now or datetime.now(UTC)).isoformat()
        client.set(_KEY_PREFIX + task_name, ts, ex=_TTL_SECONDS)
        return True
    except Exception:
        logger.debug("job_heartbeat: store failed for %s", task_name, exc_info=True)
        return False


def get_last_success(task_name: str) -> datetime | None:
    """Last successful completion time for a task, or None. Never raises."""
    client = _get_redis()
    if client is None:
        return None
    try:
        raw = client.get(_KEY_PREFIX + task_name)
        if not raw:
            return None
        if isinstance(raw, bytes):
            raw = raw.decode()
        return datetime.fromisoformat(raw)
    except Exception:
        logger.debug("job_heartbeat: load failed for %s", task_name, exc_info=True)
        return None


def record_result(
    task_name: str,
    *,
    status: str,
    detail: dict[str, Any] | None = None,
    now: datetime | None = None,
) -> bool:
    """Store the LAST-RUN result of a task as a small JSON blob. Never raises.

    Blob shape: ``{"status": "ok"|"error", "at": iso8601, "detail": {...}|None}``.
    ``detail`` is the task's returned counts on success, or ``{"error": msg}`` on
    failure. Recorded under a separate key from the last-success heartbeat so the
    two never clobber each other.
    """
    if not task_name or not status:
        return False
    client = _get_redis()
    if client is None:
        return False
    try:
        payload = {
            "status": status,
            "at": (now or datetime.now(UTC)).isoformat(),
            "detail": detail if isinstance(detail, dict) else None,
        }
        client.set(
            _RESULT_KEY_PREFIX + task_name, json.dumps(payload), ex=_TTL_SECONDS
        )
        return True
    except Exception:
        logger.debug(
            "job_heartbeat: result store failed for %s", task_name, exc_info=True
        )
        return False


def get_last_result(task_name: str) -> dict[str, Any] | None:
    """Last-run result blob for a task, or None if unset. Never raises."""
    if not task_name:
        return None
    client = _get_redis()
    if client is None:
        return None
    try:
        raw = client.get(_RESULT_KEY_PREFIX + task_name)
        if not raw:
            return None
        if isinstance(raw, bytes):
            raw = raw.decode()
        data = json.loads(raw)
        if not isinstance(data, dict):
            return None
        return data
    except Exception:
        logger.debug(
            "job_heartbeat: result load failed for %s", task_name, exc_info=True
        )
        return None
