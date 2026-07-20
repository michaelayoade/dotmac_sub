"""Reusable task heartbeats (dead-man switches for critical beat tasks).

The generic scheduled-task staleness finding (``web_system_health`` →
``admin_alerts``) already notices when a beat task stops succeeding. This
service adds the richer per-task signals that pattern can't see: consecutive
single-flight skips (a stuck lock looks "healthy" to last-success-age until
it's very late) and the last completed run's counters, so alert collectors
can judge *what the task reported*, not just *that it ran*.

Pattern (see ``infrastructure_polling`` and ``radius_health``):

- task records ``record_success(name, result)`` after a committed run and
  ``record_skip(name)`` when the single-flight lock is held;
- an ``admin_alerts`` collector reads ``snapshot(name)`` and raises findings.

Cache-only and advisory: a cache outage must never fail the task itself.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from typing import Any

logger = logging.getLogger(__name__)

_KEY_PREFIX = "task_heartbeat:"
_TTL_SECONDS = 7 * 86_400


def _success_key(task: str) -> str:
    return f"{_KEY_PREFIX}{task}:last_success"


def _skip_key(task: str) -> str:
    return f"{_KEY_PREFIX}{task}:skip_streak"


def record_success(
    task: str, result: dict | None = None, *, now: datetime | None = None
) -> None:
    """Stamp the heartbeat after a completed run; resets the skip streak.

    Only numeric values from ``result`` are kept — the snapshot is a health
    signal, not a result archive.
    """
    try:
        from app.services.app_cache import set_json

        stamp = {
            "at": (now or datetime.now(UTC)).isoformat(),
            "result": {
                k: v
                for k, v in (result or {}).items()
                if isinstance(v, (int, float)) and not isinstance(v, bool)
            },
        }
        set_json(_success_key(task), stamp, _TTL_SECONDS)
        set_json(_skip_key(task), 0, _TTL_SECONDS)
    except Exception:  # advisory; never fail the task over cache trouble
        logger.exception("task_heartbeat_write_failed task=%s", task)


def record_skip(task: str) -> int:
    """Count a consecutive single-flight skip; returns the current streak."""
    try:
        from app.services.app_cache import get_json, set_json

        streak = int(get_json(_skip_key(task)) or 0) + 1
        set_json(_skip_key(task), streak, _TTL_SECONDS)
        return streak
    except Exception:
        logger.exception("task_heartbeat_skip_write_failed task=%s", task)
        return 0


def snapshot(task: str, *, now: datetime | None = None) -> dict[str, Any]:
    """Read a task's heartbeat: last-success age, skip streak, last counters.

    ``last_success_age_seconds`` is None when no heartbeat was ever recorded
    (or the cache is unavailable) — callers decide how alarming that is.
    """
    now = now or datetime.now(UTC)
    age: float | None = None
    result: dict[str, Any] = {}
    streak = 0
    try:
        from app.services.app_cache import get_json

        stamp = get_json(_success_key(task))
        if isinstance(stamp, dict) and stamp.get("at"):
            recorded = datetime.fromisoformat(str(stamp["at"]))
            if recorded.tzinfo is None:
                recorded = recorded.replace(tzinfo=UTC)
            age = max(0.0, (now - recorded).total_seconds())
            result = dict(stamp.get("result") or {})
        streak = int(get_json(_skip_key(task)) or 0)
    except Exception:
        logger.exception("task_heartbeat_read_failed task=%s", task)
    return {
        "last_success_age_seconds": age,
        "skip_streak": streak,
        "result": result,
    }
