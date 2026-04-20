"""Rate-control boundary for API and device operations."""

from __future__ import annotations

from collections import defaultdict, deque
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from threading import Lock

from app.services.adapters import adapter_registry


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

    def check(self, rule: RateLimitRule, *, now: datetime | None = None) -> RateLimitDecision:
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


rate_limiter_adapter = InMemoryRateLimiterAdapter()
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
