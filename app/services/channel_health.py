"""Channel and queue health observations (docs/designs/CHANNEL_OBSERVABILITY.md).

Produces the two worker-side snapshots that the dead-man's-switch and the
queue-depth alerts read: per-channel inbound freshness and per-queue depth.
Both are pure reads published through the shared observability snapshot bridge,
so the web process can export them without the worker's in-memory gauges (which
it never sees). Every read is fail-soft: a broker hiccup must not stop the
freshness snapshot, and neither snapshot may ever raise into the beat task.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime, timedelta
from typing import cast

from sqlalchemy import case, func
from sqlalchemy.orm import Session

from app.services.observability import StateObservation, publish_state_snapshot

logger = logging.getLogger(__name__)

CHANNEL_INGESTION_DOMAIN = "channel_ingestion"
CELERY_QUEUES_DOMAIN = "celery_queues"

# Window for the "is this channel still producing" volume signal. Kept short so
# the count reflects the live rate, not an hour-old average.
_FRESHNESS_WINDOW = timedelta(minutes=15)


def collect_channel_ingestion_observations(
    db: Session, *, now: datetime | None = None
) -> list[StateObservation]:
    """Freshness and recent volume per inbound channel that has any history.

    A channel with no inbound row ever is omitted rather than reported as
    infinitely stale: absence of history means "unused", which is not the
    silence the alert is for. The silence alert is about a channel that was
    producing and stopped, and that channel always has a last-inbound row.
    """
    from app.models.team_inbox import InboxMessage, InboxMessageDirection

    moment = now or datetime.now(UTC)
    window_start = moment - _FRESHNESS_WINDOW
    recent = func.sum(case((InboxMessage.received_at >= window_start, 1), else_=0))
    rows = (
        db.query(
            InboxMessage.channel_type,
            func.max(InboxMessage.received_at),
            recent,
        )
        .filter(InboxMessage.direction == InboxMessageDirection.inbound.value)
        .group_by(InboxMessage.channel_type)
        .all()
    )

    observations: list[StateObservation] = []
    for channel_type, last_received, recent_count in rows:
        channel = str(channel_type or "unknown")
        if last_received is not None:
            if last_received.tzinfo is None:
                last_received = last_received.replace(tzinfo=UTC)
            age = max(0.0, (moment - last_received).total_seconds())
            observations.append(
                StateObservation("seconds_since_last_inbound", channel, age)
            )
        observations.append(
            StateObservation("inbound_count_15m", channel, float(recent_count or 0))
        )
    return observations


def collect_celery_queue_observations() -> list[StateObservation]:
    """Depth of each configured Celery queue, read as a Redis LLEN.

    The queue set comes from the Celery config, never a literal list, so a new
    queue is covered the moment it is routed. Redis is the only broker Sub runs;
    on any connection error the caller downgrades the snapshot rather than
    failing, so an unreachable broker degrades the queue signal without taking
    the freshness signal down with it.
    """
    from app.celery_app import celery_app

    queue_names = [
        queue.name
        for queue in (celery_app.conf.task_queues or [])
        if getattr(queue, "name", None)
    ]
    if not queue_names:
        return []

    import redis

    client = redis.Redis.from_url(celery_app.conf.broker_url)
    try:
        # llen is typed as a sync/async union in redis-py's stubs; this is the
        # synchronous client, so the result is a plain int.
        return [
            StateObservation("queue_depth", name, float(cast(int, client.llen(name))))
            for name in queue_names
        ]
    finally:
        try:
            client.close()
        except Exception:
            pass


def publish_channel_health(db: Session, *, now: datetime | None = None) -> dict:
    """Compute and publish both snapshots; returns a small summary for logging.

    The queue read can fail independently of the DB read, so the two snapshots
    carry their own status: freshness is ``ok`` whenever the inbox query
    succeeds, and the queue snapshot is ``degraded`` (empty) when the broker is
    unreachable. Neither failure propagates.
    """
    moment = now or datetime.now(UTC)

    ingestion = collect_channel_ingestion_observations(db, now=moment)
    publish_state_snapshot(CHANNEL_INGESTION_DOMAIN, ingestion, now=moment)

    queue_status = "ok"
    try:
        queues = collect_celery_queue_observations()
    except Exception:
        logger.warning("channel_health_queue_read_failed", exc_info=True)
        queues = []
        queue_status = "degraded"
    publish_state_snapshot(
        CELERY_QUEUES_DOMAIN, queues, status=queue_status, now=moment
    )

    return {
        "channels": len({obs.scope for obs in ingestion}),
        "queues": len(queues),
        "queue_status": queue_status,
    }
