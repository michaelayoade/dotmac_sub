"""Relay metrics (prometheus_client, the repo's metrics library).

Closed label set: none. Event types are database strings and are never used as
labels. ``dead_lettered_total`` is a process-lifetime counter, so alert on
``increase()`` over a window rather than on the absolute value.

The dispatcher role cannot read tables, so "oldest pending" is the age of the
oldest event this process observed LEFT pending (parked or awaiting retry) at
its last settle, retained for ``retain_seconds`` after it was last seen. It is
a floor on the true backlog age, not a table scan.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from datetime import UTC, datetime

from prometheus_client import Counter, Gauge

CLAIMED = Counter("outbox_relay_claimed_total", "Outbox events claimed")
SUCCEEDED = Counter("outbox_relay_succeeded_total", "Outbox events delivered")
FAILED = Counter(
    "outbox_relay_failed_total", "Handler failures (retry scheduled or dead-lettered)"
)
DEAD_LETTERED = Counter(
    "outbox_relay_dead_lettered_total", "Events dead-lettered at max attempts"
)
PARKED = Counter(
    "outbox_relay_parked_total", "Claims parked because no handler is registered"
)
LEASE_LOST = Counter(
    "outbox_relay_lease_lost_total", "Settles refused because the lease was lost"
)
CYCLE_ERRORS = Counter("outbox_relay_cycle_errors_total", "Dispatcher cycle errors")
LAST_POLL = Gauge(
    "outbox_relay_last_poll_timestamp_seconds", "Unix time of the last completed poll"
)
OLDEST_PENDING_AGE = Gauge(
    "outbox_relay_oldest_pending_age_seconds",
    "Age of the oldest event left pending (parked or retrying) in this process",
)


@dataclass(frozen=True, slots=True)
class CycleResult:
    claimed: int = 0
    succeeded: int = 0
    failed: int = 0
    dead_lettered: int = 0
    parked: int = 0
    lease_lost: int = 0
    #: created_at of the oldest event left pending by this cycle, if any.
    oldest_pending_created_at: datetime | None = None


class OldestPendingTracker:
    def __init__(self, retain_seconds: float) -> None:
        self._retain = retain_seconds
        self._created_at: datetime | None = None
        self._seen_at: datetime | None = None

    def observe(self, oldest: datetime | None, now: datetime) -> None:
        if oldest is not None:
            if self._created_at is None or oldest < self._created_at:
                self._created_at = oldest
            self._seen_at = now
        elif self._seen_at is not None and (now - self._seen_at).total_seconds() > (
            self._retain
        ):
            self._created_at = None
            self._seen_at = None

    def age_seconds(self, now: datetime) -> float:
        if self._created_at is None:
            return 0.0
        return max(0.0, (now - self._created_at).total_seconds())


def record_cycle(
    result: CycleResult, tracker: OldestPendingTracker, now: datetime | None = None
) -> None:
    moment = now or datetime.now(UTC)
    CLAIMED.inc(result.claimed)
    SUCCEEDED.inc(result.succeeded)
    FAILED.inc(result.failed)
    DEAD_LETTERED.inc(result.dead_lettered)
    PARKED.inc(result.parked)
    LEASE_LOST.inc(result.lease_lost)
    tracker.observe(result.oldest_pending_created_at, moment)
    OLDEST_PENDING_AGE.set(tracker.age_seconds(moment))
    LAST_POLL.set(time.time())
