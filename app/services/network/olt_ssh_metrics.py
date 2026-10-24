"""Metrics for the per-OLT SSH session limiter.

Celery workers cannot export Prometheus gauges to the web ``/metrics`` (see
``app/metrics.py``), so counters live in a Redis hash that a scrape-time
collector reads back, fail-soft (the same pattern as the suspension-audit
collector). The truth for *sessions held* is not a counter at all: the web
process counts held advisory locks in ``pg_locks`` at scrape time.

Nothing here may raise into the SSH path, and nothing here carries secrets.
"""

from __future__ import annotations

import logging
import os
import time
from typing import Any

from app.services.network.olt_ssh_limiter import (
    LIMITER_APPLICATION_NAME,
    LimiterConfig,
    intent_key,
    model_key_for,
    slot_key,
)

logger = logging.getLogger(__name__)

COUNTER_HASH_KEY = "olt_ssh:limiter:counters"
COUNTER_TTL_SECONDS = 30 * 24 * 3600
# Upper bounds (seconds) of the slot-wait histogram buckets.
WAIT_BUCKETS = (0.5, 1.0, 5.0, 15.0, 45.0)

OPEN_OUTCOMES = (
    "ok",
    "busy_skipped",
    "busy_timeout",
    "auth_eof",
    "auth_eof_retried_ok",
    "auth_rejected",
    "connect_timeout",
    "other",
)

_redis_client: Any = None


def _redis() -> Any:
    global _redis_client
    if _redis_client is None:
        url = os.getenv("REDIS_URL")
        if not url:
            return None
        import redis

        _redis_client = redis.Redis.from_url(
            url, socket_timeout=1, socket_connect_timeout=1
        )
    return _redis_client


_redis_down_until = 0.0


def _incr(field: str, amount: float = 1) -> None:
    global _redis_down_until
    if time.monotonic() < _redis_down_until:
        return
    try:
        client = _redis()
        if client is None:
            return
        if isinstance(amount, float):
            client.hincrbyfloat(COUNTER_HASH_KEY, field, amount)
        else:
            client.hincrby(COUNTER_HASH_KEY, field, amount)
        client.expire(COUNTER_HASH_KEY, COUNTER_TTL_SECONDS)
    except Exception:
        # Fail soft and back off so a dead Redis never slows the SSH path.
        _redis_down_until = time.monotonic() + 30.0
        logger.debug("olt_ssh_metrics_incr_failed", exc_info=True)


def record_open(olt: str, priority: str, outcome: str) -> None:
    _incr(f"open|{olt}|{priority}|{outcome}")


def record_wait(olt: str, priority: str, seconds: float) -> None:
    seconds = max(0.0, float(seconds))
    for bound in WAIT_BUCKETS:
        if seconds <= bound:
            _incr(f"wait_bucket|{olt}|{priority}|{bound}")
    _incr(f"wait_bucket|{olt}|{priority}|+Inf")
    _incr(f"wait_sum|{olt}|{priority}", seconds)


def record_logout(outcome: str) -> None:
    _incr(f"logout|{outcome}")


def classify_open_failure(exc: BaseException) -> str:
    """Map an open failure onto the bounded ``outcome`` label set."""
    from paramiko.ssh_exception import AuthenticationException

    from app.services.network.olt_ssh_limiter import (
        OltSessionBusy,
        is_auth_eof_message,
    )

    if isinstance(exc, OltSessionBusy):
        return "busy_timeout"
    if isinstance(exc, AuthenticationException):
        return "auth_rejected"
    if is_auth_eof_message(exc) or isinstance(exc, EOFError):
        return "auth_eof"
    if isinstance(exc, RuntimeError) and "authentication failed" in str(exc).lower():
        return "auth_rejected"
    if isinstance(exc, TimeoutError | OSError):
        return "connect_timeout"
    return "other"


def load_counters() -> dict[str, float]:
    try:
        client = _redis()
        if client is None:
            return {}
        raw = client.hgetall(COUNTER_HASH_KEY) or {}
    except Exception:
        return {}
    parsed: dict[str, float] = {}
    for key, value in raw.items():
        name = key.decode() if isinstance(key, bytes) else str(key)
        try:
            parsed[name] = float(value)
        except (TypeError, ValueError):
            continue
    return parsed


def _lock_halves(key: int) -> tuple[int, int]:
    """Advisory bigint key -> (classid, objid) as pg_locks reports it."""
    unsigned = key & 0xFFFFFFFFFFFFFFFF
    return unsigned >> 32, unsigned & 0xFFFFFFFF


def held_slots_by_olt(
    olts: list[tuple[str, str, str | None]], config: LimiterConfig
) -> dict[str, tuple[int, float]]:
    """Count held slot locks per OLT from ``pg_locks`` (truth, not a counter).

    ``olts`` is ``(olt_id, name, model)``. Returns ``name -> (held, oldest_age_s)``.
    """
    from sqlalchemy import text

    from app.services.db_session_adapter import db_session_adapter

    wanted: dict[tuple[int, int], str] = {}
    for olt_id, name, model in olts:
        limit = config.limit_for(model_key_for(model))
        for index in range(max(limit, 1)):
            wanted[_lock_halves(slot_key(olt_id, index))] = name
    if not wanted:
        return {}
    with db_session_adapter.read_session() as db:
        if not db.get_bind().dialect.name.startswith("postgres"):
            return {}
        rows = db.execute(
            text(
                "SELECT l.classid::bigint, l.objid::bigint, "
                "EXTRACT(EPOCH FROM (now() - a.backend_start)) "
                "FROM pg_locks l JOIN pg_stat_activity a ON a.pid = l.pid "
                "WHERE l.locktype = 'advisory' AND l.granted AND l.objsubid = 1 "
                "AND a.application_name = :app"
            ),
            {"app": LIMITER_APPLICATION_NAME},
        ).all()
    held: dict[str, tuple[int, float]] = {}
    for classid, objid, age in rows:
        name = wanted.get((int(classid), int(objid)))
        if name is None:
            continue
        count, oldest = held.get(name, (0, 0.0))
        held[name] = (count + 1, max(oldest, float(age or 0.0)))
    return held


__all__ = [
    "COUNTER_HASH_KEY",
    "WAIT_BUCKETS",
    "classify_open_failure",
    "held_slots_by_olt",
    "intent_key",
    "load_counters",
    "model_key_for",
    "record_logout",
    "record_open",
    "record_wait",
]
