"""Metrics for the per-OLT SSH session limiter.

Workers cannot export Prometheus gauges to the web ``/metrics`` and a scrape
must never touch the database (``docs/METRICS_SCRAPE_SAFETY.md``). So:

* hot-path counters (opens by outcome, slot wait, logouts) are incremented
  fail-soft in a Redis hash by whichever process opens the session;
* a scheduled producer (``publish_olt_ssh_snapshot``, run from the existing
  infrastructure check) counts held slots from ``pg_locks`` (the truth for
  *sessions per OLT*), adds the counters, and publishes one bounded
  ``olt_ssh`` state snapshot that the existing observability collector exports
  as ``observability_state{domain="olt_ssh",signal,scope}``.

Nothing here may raise into the SSH path, and nothing here carries secrets.
"""

from __future__ import annotations

import logging
import re
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
    "login_skipped",
    "connect_timeout",
    "other",
)


def _redis() -> Any:
    from app.services.redis_client import get_redis

    return get_redis()


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


def record_host_key_mismatch(olt: str) -> None:
    """Counter ``olt_ssh_host_key_mismatch`` (published in the olt_ssh snapshot)."""
    _incr(f"host_key_mismatch|{olt}")


def host_key_mismatch_olts(db: Any) -> int:
    """Count active OLTs currently presenting a key other than the pinned one."""
    from sqlalchemy import func, select

    from app.models.network import OLTDevice

    return int(
        db.scalar(
            select(func.count())
            .select_from(OLTDevice)
            .where(
                OLTDevice.is_active.is_(True),
                OLTDevice.olt_ssh_host_key_sha256.is_not(None),
                # A presented key that differs from the pinned one; cleared by a
                # matching login or by an explicit trust, so the gauge drops.
                OLTDevice.olt_ssh_presented_key_sha256.is_not(None),
            )
        )
        or 0
    )


def classify_open_failure(exc: BaseException) -> str:
    """Map an open failure onto the bounded ``outcome`` label set."""
    from paramiko.ssh_exception import AuthenticationException

    from app.services.network.olt_ssh_limiter import (
        OltSessionBusy,
        is_auth_eof_message,
        is_login_refusal_message,
    )

    if isinstance(exc, OltSessionBusy):
        return "busy_timeout"
    if isinstance(exc, AuthenticationException):
        return "auth_rejected"
    if (
        is_auth_eof_message(exc)
        or is_login_refusal_message(exc)
        or isinstance(exc, EOFError)
    ):
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
    db: Any, olts: list[tuple[str, str, str | None]], config: LimiterConfig
) -> dict[str, tuple[int, float]]:
    """Count held slot locks per OLT from ``pg_locks`` (truth, not a counter).

    ``olts`` is ``(olt_id, name, model)``. Returns ``name -> (held, oldest_age_s)``.
    """
    from sqlalchemy import text

    wanted: dict[tuple[int, int], str] = {}
    for olt_id, name, model in olts:
        limit = config.limit_for(model_key_for(model))
        for index in range(max(limit, 1)):
            wanted[_lock_halves(slot_key(olt_id, index))] = name
    if not wanted:
        return {}
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
        olt_name = wanted.get((int(classid), int(objid)))
        if olt_name is None:
            continue
        count, oldest = held.get(olt_name, (0, 0.0))
        held[olt_name] = (count + 1, max(oldest, float(age or 0.0)))
    return held


_SCOPE_UNSAFE = re.compile(r"[^A-Za-z0-9_.:-]+")
_MAX_OLTS = 14
_PER_OLT_SIGNALS = 12  # sessions_held, oldest_hold, 8 open outcomes, 2 wait totals


def _scope(name: str) -> str:
    return _SCOPE_UNSAFE.sub("_", str(name)).strip("_")[:100] or "olt"


def publish_olt_ssh_snapshot(db: Any, *, now: Any = None) -> bool:
    """Publish held-slot truth plus counters as the ``olt_ssh`` state snapshot.

    Runs in a worker (never in a scrape). Bounded: at most ``_MAX_OLTS`` OLTs.
    """
    from sqlalchemy import select

    from app.models.network import OLTDevice
    from app.services.network.olt_ssh_limiter import get_config
    from app.services.observability import StateObservation, publish_state_snapshot

    rows = db.execute(
        select(OLTDevice.id, OLTDevice.name, OLTDevice.model)
        .where(OLTDevice.is_active.is_(True))
        .order_by(OLTDevice.name)
        .limit(_MAX_OLTS)
    ).all()
    olts = [(str(row.id), row.name, row.model) for row in rows]
    held = held_slots_by_olt(db, olts, get_config())
    counters = load_counters()

    observations: list[Any] = []
    for _olt_id, name, _model in olts:
        scope = _scope(name)
        count, oldest = held.get(name, (0, 0.0))
        observations.append(StateObservation("sessions_held", scope, float(count)))
        observations.append(StateObservation("oldest_hold_seconds", scope, oldest))
        # Alert hook (design 5d): 1 when a slot has been held over 900 s.
        observations.append(
            StateObservation("slot_hold_over_900s", scope, 1.0 if oldest > 900 else 0.0)
        )
        for outcome in OPEN_OUTCOMES:
            total = sum(
                value
                for key, value in counters.items()
                if key.startswith("open|")
                and key.split("|")[1] == name
                and key.split("|")[-1] == outcome
            )
            observations.append(StateObservation(f"open_{outcome}", scope, total))
        observations.append(
            StateObservation(
                "slot_wait_seconds_sum",
                scope,
                sum(
                    value
                    for key, value in counters.items()
                    if key.startswith("wait_sum|") and key.split("|")[1] == name
                ),
            )
        )
        observations.append(
            StateObservation(
                "slot_wait_count",
                scope,
                sum(
                    value
                    for key, value in counters.items()
                    if key.startswith("wait_bucket|")
                    and key.split("|")[1] == name
                    and key.split("|")[-1] == "+Inf"
                ),
            )
        )
    # One bounded fleet-wide series: the snapshot is capped, so no per-OLT label.
    observations.append(
        StateObservation(
            "olt_ssh_host_key_mismatch",
            "all",
            sum(v for k, v in counters.items() if k.startswith("host_key_mismatch|")),
        )
    )
    # Gauge from the database truth: OLTs whose recorded key was contradicted
    # and not yet explicitly trusted (trusting clears last_mismatch_at).
    observations.append(
        StateObservation(
            "olt_ssh_host_key_mismatch_olts", "all", float(host_key_mismatch_olts(db))
        )
    )
    for outcome in ("clean", "forced", "skipped_dirty"):
        observations.append(
            StateObservation(
                f"logout_{outcome}", "all", counters.get(f"logout|{outcome}", 0.0)
            )
        )
    return publish_state_snapshot("olt_ssh", observations, now=now)


__all__ = [
    "COUNTER_HASH_KEY",
    "WAIT_BUCKETS",
    "classify_open_failure",
    "held_slots_by_olt",
    "intent_key",
    "load_counters",
    "model_key_for",
    "publish_olt_ssh_snapshot",
    "record_logout",
    "record_open",
    "record_wait",
]
