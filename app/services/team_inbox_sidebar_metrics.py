"""Bounded, non-authoritative cache for shared Inbox sidebar metrics.

Conversation and message rows remain authoritative. Shared queue metrics are
fresh for 15 seconds; actor-scoped assignment totals are fresh for 30 seconds.
Both may be served stale for five minutes and are coordinated across web
processes through the existing application-cache Redis connection. A
process-local snapshot and lease preserve the same bounded behavior when Redis
is unavailable.
"""

from __future__ import annotations

import logging
import secrets
from collections.abc import Sequence
from dataclasses import asdict, dataclass
from datetime import UTC, datetime, timedelta
from hashlib import sha256
from threading import Lock
from typing import Protocol, cast
from uuid import UUID

from redis.exceptions import RedisError
from sqlalchemy import text
from sqlalchemy.orm import Session

from app.config import settings
from app.services import app_cache, team_inbox_operations, team_inbox_read

logger = logging.getLogger(__name__)

FRESH_SECONDS = 15
ASSIGNMENT_FRESH_SECONDS = 30
MAX_STALE_SECONDS = 300
REFRESH_LEASE_SECONDS = 125
_CACHE_KEY = app_cache.cache_key("inbox", "sidebar_metrics", "v1")
_LEASE_KEY = app_cache.cache_key("inbox", "sidebar_metrics", "refresh", "v1")
_ASSIGNMENT_CACHE_VERSION = "v1"
_MAX_LOCAL_ASSIGNMENT_SNAPSHOTS = 1024


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


@dataclass(frozen=True, slots=True)
class InboxAssignmentMetricsSnapshot:
    counts: team_inbox_read.InboxAssignmentCohortCounts
    observed_at: datetime

    def is_fresh(self, now: datetime) -> bool:
        return now - self.observed_at <= timedelta(seconds=ASSIGNMENT_FRESH_SECONDS)

    def is_servable(self, now: datetime) -> bool:
        return now - self.observed_at <= timedelta(seconds=MAX_STALE_SECONDS)


_fallback_lock = Lock()
_fallback_snapshot: InboxSidebarMetricsSnapshot | None = None
_fallback_retry_after: datetime | None = None
_assignment_fallback_lock = Lock()
_assignment_state_lock = Lock()
_assignment_fallback_snapshots: dict[str, InboxAssignmentMetricsSnapshot] = {}


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


def _assignment_scope(
    actor_person_id: UUID | None,
    actor_team_ids: Sequence[UUID],
) -> str:
    identity = "|".join(
        (
            str(actor_person_id or "anonymous"),
            *(str(value) for value in sorted(actor_team_ids, key=str)),
        )
    )
    return sha256(identity.encode("utf-8")).hexdigest()[:24]


def _assignment_keys(scope: str) -> tuple[str, str]:
    return (
        app_cache.cache_key(
            "inbox", "assignment_metrics", scope, _ASSIGNMENT_CACHE_VERSION
        ),
        app_cache.cache_key(
            "inbox",
            "assignment_metrics",
            scope,
            "refresh",
            _ASSIGNMENT_CACHE_VERSION,
        ),
    )


def _assignment_snapshot_from_payload(
    payload: object,
) -> InboxAssignmentMetricsSnapshot | None:
    if not isinstance(payload, dict):
        return None
    counts_payload = payload.get("counts")
    if not isinstance(counts_payload, dict):
        return None
    try:
        observed_at = datetime.fromisoformat(str(payload["observed_at"]))
        if observed_at.tzinfo is None:
            observed_at = observed_at.replace(tzinfo=UTC)
        counts = team_inbox_read.InboxAssignmentCohortCounts(
            all=max(0, int(counts_payload["all"])),
            queued=max(0, int(counts_payload["queued"])),
            assigned_to_actor=max(0, int(counts_payload["assigned_to_actor"])),
            actor_teams=max(0, int(counts_payload["actor_teams"])),
            ai_handling=max(0, int(counts_payload["ai_handling"])),
        )
        return InboxAssignmentMetricsSnapshot(
            counts=counts,
            observed_at=observed_at.astimezone(UTC),
        )
    except (KeyError, TypeError, ValueError):
        return None


def _assignment_snapshot_payload(
    snapshot: InboxAssignmentMetricsSnapshot,
) -> dict[str, object]:
    return {
        "counts": asdict(snapshot.counts),
        "observed_at": snapshot.observed_at.isoformat(),
    }


def _remember_assignment_snapshot(
    scope: str,
    snapshot: InboxAssignmentMetricsSnapshot,
) -> None:
    with _assignment_state_lock:
        if (
            scope not in _assignment_fallback_snapshots
            and len(_assignment_fallback_snapshots) >= _MAX_LOCAL_ASSIGNMENT_SNAPSHOTS
        ):
            oldest_scope = min(
                _assignment_fallback_snapshots,
                key=lambda key: _assignment_fallback_snapshots[key].observed_at,
            )
            _assignment_fallback_snapshots.pop(oldest_scope, None)
        _assignment_fallback_snapshots[scope] = snapshot


def _compute_snapshot(db: Session, now: datetime) -> InboxSidebarMetricsSnapshot:
    return InboxSidebarMetricsSnapshot(
        queue_metrics=team_inbox_operations.queue_metrics(db),
        needs_attention=team_inbox_read.needs_attention_conversation_count(db),
        observed_at=now,
    )


def _release_lease(
    client: _LeaseClient,
    token: str,
    *,
    lease_key: str = _LEASE_KEY,
) -> None:
    compare_and_delete = (
        "if redis.call('get', KEYS[1]) == ARGV[1] then "
        "return redis.call('del', KEYS[1]) else return 0 end"
    )
    try:
        client.eval(compare_and_delete, 1, lease_key, token)
    except RedisError as exc:
        logger.warning(
            "inbox_sidebar_metrics_lease_release_failed error=%s",
            type(exc).__name__,
        )


def get_assignment_cohort_counts(
    db: Session,
    *,
    actor_person_id: UUID | None,
    actor_team_ids: Sequence[UUID],
    now: datetime | None = None,
) -> team_inbox_read.InboxAssignmentCohortCounts | None:
    """Return briefly cached actor-scoped totals, or ``None`` when busy and cold."""

    current = (now or datetime.now(UTC)).astimezone(UTC)
    get_bind = getattr(db, "get_bind", None)
    if callable(get_bind) and get_bind().dialect.name != "postgresql":
        return team_inbox_read.assignment_cohort_counts(
            db,
            actor_person_id=actor_person_id,
            actor_team_ids=actor_team_ids,
        )

    scope = _assignment_scope(actor_person_id, actor_team_ids)
    cache_key, lease_key = _assignment_keys(scope)
    local = _assignment_fallback_snapshots.get(scope)
    if local is not None and local.is_fresh(current):
        return local.counts

    raw_client = app_cache.get_cache_redis()
    shared = (
        _assignment_snapshot_from_payload(app_cache.get_json(cache_key))
        if raw_client is not None
        else None
    )
    cached = max(
        (item for item in (shared, local) if item is not None),
        key=lambda item: item.observed_at,
        default=None,
    )
    if cached is not None and cached.is_fresh(current):
        _remember_assignment_snapshot(scope, cached)
        return cached.counts

    lease_client = cast(_LeaseClient, raw_client) if raw_client is not None else None
    token = secrets.token_urlsafe(24)
    claimed = False
    if lease_client is not None:
        try:
            claimed = bool(
                lease_client.set(
                    lease_key,
                    token,
                    nx=True,
                    ex=REFRESH_LEASE_SECONDS,
                )
            )
        except RedisError as exc:
            logger.warning(
                "inbox_assignment_metrics_lease_claim_failed error=%s",
                type(exc).__name__,
            )
            lease_client = None
    if lease_client is None:
        claimed = _assignment_fallback_lock.acquire(blocking=False)

    if not claimed:
        return (
            cached.counts
            if cached is not None and cached.is_servable(current)
            else None
        )

    refresh_succeeded = False
    try:
        counts = team_inbox_read.assignment_cohort_counts(
            db,
            actor_person_id=actor_person_id,
            actor_team_ids=actor_team_ids,
        )
        snapshot = InboxAssignmentMetricsSnapshot(counts=counts, observed_at=current)
        _remember_assignment_snapshot(scope, snapshot)
        app_cache.set_json(
            cache_key,
            _assignment_snapshot_payload(snapshot),
            ttl_seconds=MAX_STALE_SECONDS,
        )
        refresh_succeeded = True
        return counts
    finally:
        if lease_client is not None and refresh_succeeded:
            _release_lease(lease_client, token, lease_key=lease_key)
        elif lease_client is None:
            _assignment_fallback_lock.release()


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
