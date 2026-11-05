"""Bounded, non-authoritative cache for shared Inbox sidebar metrics.

Conversation and message rows remain authoritative. This projection is
deterministically rebuilt from those rows, is fresh for 15 seconds, may be
served stale for five minutes, and is coordinated across web processes through
the existing application-cache Redis connection. A process-local snapshot and
lease preserve the same bounded behavior when Redis is unavailable.
"""

from __future__ import annotations

import logging
import secrets
from dataclasses import asdict, dataclass
from datetime import UTC, datetime, timedelta
from threading import Lock
from typing import Protocol, cast

from redis.exceptions import RedisError
from sqlalchemy import text
from sqlalchemy.orm import Session

from app.config import settings
from app.services import app_cache, team_inbox_operations, team_inbox_read

logger = logging.getLogger(__name__)

FRESH_SECONDS = 15
MAX_STALE_SECONDS = 300
REFRESH_LEASE_SECONDS = 125
_CACHE_KEY = app_cache.cache_key("inbox", "sidebar_metrics", "v1")
_LEASE_KEY = app_cache.cache_key("inbox", "sidebar_metrics", "refresh", "v1")


class _LeaseClient(Protocol):
    def set(self, name: str, value: str, *, nx: bool, ex: int) -> object: ...

    def eval(self, script: str, numkeys: int, *keys_and_args: str) -> object: ...


@dataclass(frozen=True, slots=True)
class InboxSidebarMetricsSnapshot:
    queue_metrics: team_inbox_operations.InboxQueueMetrics
    needs_attention: int
    observed_at: datetime

    def is_fresh(self, now: datetime) -> bool:
        return now - self.observed_at <= timedelta(seconds=FRESH_SECONDS)

    def is_servable(self, now: datetime) -> bool:
        return now - self.observed_at <= timedelta(seconds=MAX_STALE_SECONDS)


_fallback_lock = Lock()
_fallback_snapshot: InboxSidebarMetricsSnapshot | None = None
_fallback_retry_after: datetime | None = None


def apply_read_budget(db: Session) -> None:
    """Bound interactive Inbox SQL below the browser request deadline."""

    if db.get_bind().dialect.name != "postgresql":
        return
    timeout_ms = max(
        1000,
        min(int(settings.inbox_read_statement_timeout_ms), 14000),
    )
    db.execute(
        text("SELECT set_config('statement_timeout', :timeout, true)"),
        {"timeout": f"{timeout_ms}ms"},
    )


def _unavailable_snapshot(now: datetime) -> InboxSidebarMetricsSnapshot:
    return InboxSidebarMetricsSnapshot(
        queue_metrics=team_inbox_operations.InboxQueueMetrics(
            total_open=0,
            needs_response=0,
            failed_outbound=0,
            unassigned_open=0,
            muted_open=0,
            snoozed_open=0,
        ),
        needs_attention=0,
        observed_at=now,
    )


def _snapshot_from_payload(payload: object) -> InboxSidebarMetricsSnapshot | None:
    if not isinstance(payload, dict):
        return None
    queue_payload = payload.get("queue_metrics")
    if not isinstance(queue_payload, dict):
        return None
    try:
        observed_at = datetime.fromisoformat(str(payload["observed_at"]))
        if observed_at.tzinfo is None:
            observed_at = observed_at.replace(tzinfo=UTC)
        queue_metrics = team_inbox_operations.InboxQueueMetrics(
            total_open=max(0, int(queue_payload["total_open"])),
            needs_response=max(0, int(queue_payload["needs_response"])),
            failed_outbound=max(0, int(queue_payload["failed_outbound"])),
            unassigned_open=max(0, int(queue_payload["unassigned_open"])),
            muted_open=max(0, int(queue_payload["muted_open"])),
            snoozed_open=max(0, int(queue_payload["snoozed_open"])),
        )
        return InboxSidebarMetricsSnapshot(
            queue_metrics=queue_metrics,
            needs_attention=max(0, int(payload["needs_attention"])),
            observed_at=observed_at.astimezone(UTC),
        )
    except (KeyError, TypeError, ValueError):
        return None


def _snapshot_payload(snapshot: InboxSidebarMetricsSnapshot) -> dict[str, object]:
    return {
        "queue_metrics": asdict(snapshot.queue_metrics),
        "needs_attention": snapshot.needs_attention,
        "observed_at": snapshot.observed_at.isoformat(),
    }


def _compute_snapshot(db: Session, now: datetime) -> InboxSidebarMetricsSnapshot:
    return InboxSidebarMetricsSnapshot(
        queue_metrics=team_inbox_operations.queue_metrics(db),
        needs_attention=team_inbox_read.needs_attention_conversation_count(db),
        observed_at=now,
    )


def _release_lease(client: _LeaseClient, token: str) -> None:
    compare_and_delete = (
        "if redis.call('get', KEYS[1]) == ARGV[1] then "
        "return redis.call('del', KEYS[1]) else return 0 end"
    )
    try:
        client.eval(compare_and_delete, 1, _LEASE_KEY, token)
    except RedisError as exc:
        logger.warning(
            "inbox_sidebar_metrics_lease_release_failed error=%s",
            type(exc).__name__,
        )


def get_sidebar_metrics(
    db: Session,
    *,
    now: datetime | None = None,
) -> InboxSidebarMetricsSnapshot:
    """Return one shared snapshot without allowing a refresh stampede."""

    global _fallback_retry_after, _fallback_snapshot

    current = (now or datetime.now(UTC)).astimezone(UTC)
    get_bind = getattr(db, "get_bind", None)
    if callable(get_bind) and get_bind().dialect.name != "postgresql":
        return _compute_snapshot(db, current)
    local = _fallback_snapshot
    if local is not None and local.is_fresh(current):
        return local
    raw_client = app_cache.get_cache_redis()
    shared = (
        _snapshot_from_payload(app_cache.get_json(_CACHE_KEY))
        if raw_client is not None
        else None
    )
    cached = max(
        (item for item in (shared, local) if item is not None),
        key=lambda item: item.observed_at,
        default=None,
    )
    if cached is not None and cached.is_fresh(current):
        return cached

    lease_client = cast(_LeaseClient, raw_client) if raw_client is not None else None
    token = secrets.token_urlsafe(24)
    claimed = False
    if lease_client is not None:
        try:
            claimed = bool(
                lease_client.set(
                    _LEASE_KEY,
                    token,
                    nx=True,
                    ex=REFRESH_LEASE_SECONDS,
                )
            )
        except RedisError as exc:
            logger.warning(
                "inbox_sidebar_metrics_lease_claim_failed error=%s",
                type(exc).__name__,
            )
            lease_client = None
    if lease_client is None:
        if _fallback_retry_after is not None and current < _fallback_retry_after:
            if cached is not None and cached.is_servable(current):
                return cached
            logger.warning("inbox_sidebar_metrics_local_circuit_open")
            return _unavailable_snapshot(current)
        claimed = _fallback_lock.acquire(blocking=False)

    if not claimed:
        if cached is not None and cached.is_servable(current):
            return cached
        logger.warning("inbox_sidebar_metrics_temporarily_unavailable")
        return _unavailable_snapshot(current)

    refresh_succeeded = False
    try:
        snapshot = _compute_snapshot(db, current)
        _fallback_snapshot = snapshot
        _fallback_retry_after = None
        app_cache.set_json(
            _CACHE_KEY,
            _snapshot_payload(snapshot),
            ttl_seconds=MAX_STALE_SECONDS,
        )
        refresh_succeeded = True
        return snapshot
    except Exception:
        if lease_client is None:
            _fallback_retry_after = current + timedelta(seconds=REFRESH_LEASE_SECONDS)
        raise
    finally:
        if lease_client is not None and refresh_succeeded:
            _release_lease(lease_client, token)
        elif lease_client is None:
            _fallback_lock.release()
