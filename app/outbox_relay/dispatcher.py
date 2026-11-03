"""Claim, route, settle. Mechanical: policy lives in the kernel and the registry.

Per cycle: claim a batch and commit (the lease becomes durable), then for each
event either deliver through its registered handler or park it.

- registered handler returns   -> ``record_success`` (sent)
- registered handler raises    -> ``record_failure`` (backoff, or dead at
  ``max_attempts``)
- NO registered handler        -> park: back to ``pending`` with a later
  ``available_at``, attempts UNCHANGED, never dead-lettered
- the settle itself fails      -> rollback; the lease expires and the row is
  reclaimed (at-least-once, as the kernel documents)
"""

from __future__ import annotations

import logging
import threading
from collections.abc import Callable
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta

from dotmac_kernel.messaging.relay import (
    ClaimedEvent,
    RelayPolicy,
    record_failure,
    record_success,
)
from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from app.outbox_relay.metrics import (
    CYCLE_ERRORS,
    CycleResult,
    OldestPendingTracker,
    record_cycle,
)
from app.outbox_relay.registry import OutboxHandlerRegistry

logger = logging.getLogger("app.outbox_relay")

SessionFactory = Callable[[], Session]

_PARK_REASON = "no handler registered for event type"


@dataclass(frozen=True, slots=True)
class ClaimedRow:
    event: ClaimedEvent
    created_at: datetime


def claim_rows(db: Session, *, worker_id: str, policy: RelayPolicy) -> list[ClaimedRow]:
    """Same function and arguments as the kernel's ``claim_batch``, but also
    returns ``created_at`` (the function returns the whole row) so the
    oldest-pending age can be measured without any table privilege."""
    rows = (
        db.execute(
            text(
                "SELECT id, tenant_id, event_type, payload, attempts, "
                "correlation_id, created_at "
                "FROM claim_outbox_batch(:w, :b, :s)"
            ),
            {
                "w": worker_id,
                "b": policy.batch_size,
                "s": policy.stale_lease_seconds,
            },
        )
        .mappings()
        .all()
    )
    return [
        ClaimedRow(
            event=ClaimedEvent(
                id=r["id"],
                tenant_id=r["tenant_id"],
                event_type=r["event_type"],
                payload=dict(r["payload"] or {}),
                attempts=r["attempts"],
                correlation_id=r["correlation_id"],
            ),
            created_at=r["created_at"],
        )
        for r in rows
    ]


def park_event(
    db: Session,
    *,
    event: ClaimedEvent,
    worker_id: str,
    available_at: datetime,
) -> bool:
    """Return a claimed event to ``pending`` without touching ``attempts``."""
    return bool(
        db.execute(
            text("SELECT settle_outbox_event(:id, :w, 'pending', :ts, :a, :e)"),
            {
                "id": str(event.id),
                "w": worker_id,
                "ts": available_at,
                "a": event.attempts,
                "e": _PARK_REASON,
            },
        ).scalar()
    )


def _older(a: datetime | None, b: datetime) -> datetime:
    return b if a is None or b < a else a


def run_cycle(
    db: Session,
    *,
    registry: OutboxHandlerRegistry,
    policy: RelayPolicy,
    worker_id: str,
    park_seconds: int,
    now: Callable[[], datetime] = lambda: datetime.now(UTC),
) -> CycleResult:
    rows = claim_rows(db, worker_id=worker_id, policy=policy)
    db.commit()  # lease durable before any delivery
    result = CycleResult(claimed=len(rows))
    oldest_pending: datetime | None = None
    for row in rows:
        event = row.event
        handler = registry.get(event.event_type)
        try:
            if handler is None:
                held = park_event(
                    db,
                    event=event,
                    worker_id=worker_id,
                    available_at=now() + timedelta(seconds=park_seconds),
                )
                db.commit()
                result = replace(
                    result,
                    parked=result.parked + (1 if held else 0),
                    lease_lost=result.lease_lost + (0 if held else 1),
                )
                oldest_pending = _older(oldest_pending, row.created_at)
                logger.info("outbox event parked (no handler) id=%s", event.id)
                continue
            try:
                handler(event)
            except Exception as exc:  # handler failure: retry or dead-letter
                outcome = record_failure(
                    db,
                    event_id=event.id,
                    worker_id=worker_id,
                    attempts=event.attempts,
                    # class name only: exception text can carry payload data
                    error=type(exc).__name__,
                    policy=policy,
                )
                db.commit()
                result = replace(
                    result,
                    failed=result.failed + 1,
                    dead_lettered=result.dead_lettered
                    + (1 if outcome.dead_lettered else 0),
                )
                if not outcome.dead_lettered:
                    oldest_pending = _older(oldest_pending, row.created_at)
                logger.warning(
                    "outbox handler failed id=%s type=%s dead=%s",
                    event.id,
                    type(exc).__name__,
                    outcome.dead_lettered,
                )
                continue
            held = record_success(db, event_id=event.id, worker_id=worker_id)
            db.commit()
            result = replace(
                result,
                succeeded=result.succeeded + (1 if held else 0),
                lease_lost=result.lease_lost + (0 if held else 1),
            )
        except SQLAlchemyError:
            db.rollback()
            logger.exception("outbox settle failed id=%s; lease will expire", event.id)
    return replace(result, oldest_pending_created_at=oldest_pending)


def run_forever(
    *,
    session_factory: SessionFactory,
    registry: OutboxHandlerRegistry,
    policy: RelayPolicy,
    worker_id: str,
    park_seconds: int,
    poll_interval_seconds: int,
    tracker: OldestPendingTracker,
    stop: threading.Event,
    heartbeat: Callable[[], None],
) -> None:
    logger.info("outbox relay %s starting", worker_id)
    while not stop.is_set():
        claimed = 0
        db = session_factory()
        try:
            result = run_cycle(
                db,
                registry=registry,
                policy=policy,
                worker_id=worker_id,
                park_seconds=park_seconds,
            )
            claimed = result.claimed
            record_cycle(result, tracker)
        except Exception:
            CYCLE_ERRORS.inc()
            db.rollback()
            logger.exception("outbox relay cycle failed")
        finally:
            db.close()
        heartbeat()
        # A full batch means more is probably ready: do not sleep.
        if claimed < policy.batch_size:
            stop.wait(poll_interval_seconds)
    logger.info("outbox relay %s stopped", worker_id)
