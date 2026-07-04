"""Rate-control boundary for API and device operations."""

from __future__ import annotations

import logging
from collections import defaultdict, deque
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from threading import Lock
from typing import cast

from app.services.adapters import adapter_registry

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class RateLimitRule:
    key: str
    limit: int
    window_seconds: int


@dataclass(frozen=True)
class RateLimitDecision:
    allowed: bool
    key: str
    limit: int
    remaining: int
    reset_at: datetime
    retry_after_seconds: int | None = None


class InMemoryRateLimiterAdapter:
    """Simple local sliding-window limiter.

    This is intentionally small. A Redis/device-vendor implementation can keep
    the same ``check`` contract and replace this backend where shared limits are
    required across workers.
    """

    name = "rate_limiter.memory"

    def __init__(self) -> None:
        self._hits: dict[str, deque[datetime]] = defaultdict(deque)
        self._lock = Lock()

    def check(
        self, rule: RateLimitRule, *, now: datetime | None = None
    ) -> RateLimitDecision:
        now = now or datetime.now(UTC)
        if now.tzinfo is None:
            now = now.replace(tzinfo=UTC)
        window = timedelta(seconds=max(rule.window_seconds, 1))
        cutoff = now - window
        with self._lock:
            hits = self._hits[rule.key]
            while hits and hits[0] <= cutoff:
                hits.popleft()
            if len(hits) >= rule.limit:
                reset_at = hits[0] + window
                retry_after = max(1, int((reset_at - now).total_seconds()))
                return RateLimitDecision(
                    allowed=False,
                    key=rule.key,
                    limit=rule.limit,
                    remaining=0,
                    reset_at=reset_at,
                    retry_after_seconds=retry_after,
                )
            hits.append(now)
            reset_at = hits[0] + window
            remaining = max(rule.limit - len(hits), 0)
            return RateLimitDecision(
                allowed=True,
                key=rule.key,
                limit=rule.limit,
                remaining=remaining,
                reset_at=reset_at,
            )


class RedisRateLimiterAdapter:
    """Fixed-window limiter shared across workers via Redis.

    Same ``check`` contract as the in-memory limiter. When Redis is
    unreachable it falls back to a per-worker in-memory limiter — throttling
    still applies (just per-worker, not globally), rather than failing open and
    letting brute-force through, or failing closed and locking everyone out.
    """

    name = "rate_limiter.redis"

    def __init__(self, fallback: InMemoryRateLimiterAdapter) -> None:
        self._fallback = fallback

    def check(
        self, rule: RateLimitRule, *, now: datetime | None = None
    ) -> RateLimitDecision:
        import redis

        from app.services.redis_client import get_redis

        client = get_redis()
        if client is None:
            return self._fallback.check(rule, now=now)

        now = now or datetime.now(UTC)
        if now.tzinfo is None:
            now = now.replace(tzinfo=UTC)
        window = max(rule.window_seconds, 1)
        bucket = int(now.timestamp() // window)
        redis_key = f"ratelimit:{rule.key}:{bucket}"
        try:
            # redis-py stubs type incr() as Awaitable[Any] | Any on the sync
            # client; cast mirrors app/services/auth.py's rate-limit path.
            count = int(cast(int, client.incr(redis_key)))
            if count == 1:
                client.expire(redis_key, window)
        except redis.RedisError:
            logger.warning(
                "Rate limiter Redis error for %s; using per-worker fallback",
                rule.key,
                exc_info=True,
            )
            return self._fallback.check(rule, now=now)

        reset_at = datetime.fromtimestamp((bucket + 1) * window, tz=UTC)
        if count > rule.limit:
            retry_after = max(1, int((reset_at - now).total_seconds()))
            return RateLimitDecision(
                allowed=False,
                key=rule.key,
                limit=rule.limit,
                remaining=0,
                reset_at=reset_at,
                retry_after_seconds=retry_after,
            )
        return RateLimitDecision(
            allowed=True,
            key=rule.key,
            limit=rule.limit,
            remaining=max(rule.limit - count, 0),
            reset_at=reset_at,
        )


_in_memory_rate_limiter = InMemoryRateLimiterAdapter()
# Shared across workers when Redis is up; per-worker in-memory otherwise.
rate_limiter_adapter = RedisRateLimiterAdapter(_in_memory_rate_limiter)
adapter_registry.register(rate_limiter_adapter)


def allow_operation(
    key: str,
    *,
    limit: int,
    window_seconds: int,
    now: datetime | None = None,
) -> RateLimitDecision:
    return rate_limiter_adapter.check(
        RateLimitRule(key=key, limit=limit, window_seconds=window_seconds),
        now=now,
    )
