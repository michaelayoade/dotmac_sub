"""Read-only database-side backlog of the composed-module outbox (SUB-2).

The relay dispatcher role cannot read tables, so the backlog is observed from
the application's own tenant-scoped session. This answers "events are queued and
nobody is draining them" independently of whether the relay process is running.
It never writes and owns no delivery decision.
"""

from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy import text
from sqlalchemy.orm import Session


@dataclass(frozen=True, slots=True)
class OutboxBacklog:
    pending: int
    claimed: int
    dead: int
    oldest_pending_age_seconds: float


_BACKLOG = text(
    """
    SELECT
      count(*) FILTER (WHERE status = 'pending') AS pending,
      count(*) FILTER (WHERE status = 'claimed') AS claimed,
      count(*) FILTER (WHERE status = 'dead') AS dead,
      COALESCE(EXTRACT(EPOCH FROM now() - min(created_at)
        FILTER (WHERE status IN ('pending', 'claimed'))), 0) AS oldest_age
    FROM public.outbox_events
    """
)


def read_outbox_backlog(db: Session) -> OutboxBacklog:
    row = db.execute(_BACKLOG).one()
    return OutboxBacklog(
        pending=int(row.pending),
        claimed=int(row.claimed),
        dead=int(row.dead),
        oldest_pending_age_seconds=max(0.0, float(row.oldest_age)),
    )
