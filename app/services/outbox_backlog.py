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

from app.services.operator_tenant import apply_operator_tenant_transaction_scope


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
    """Count the operator tenant's backlog under the runtime role.

    ``outbox_events`` is FORCE-RLS, so the runtime role sees only the rows of
    the ``app.current_tenant`` in force. Sub is a single-operator deployment
    (ADR-0009) and the runtime role cannot read ``public.tenants``, so the
    operator tenant is installed explicitly (transaction-local, discarded by
    the caller's rollback) rather than enumerated. No grant or function is
    added. The scope is installed by ``apply_operator_tenant_transaction_scope``,
    the single owner, which reads the value back.

    A second tenant is a defect under ADR-0009 and would be invisible to all of
    Sub through the root ``after_begin`` hook, not only to this read. The
    relay's own counters (``OutboxRelayDeadLettered``, ``OldestPendingAgeHigh``)
    come from the cross-tenant claim and would still see it; only
    ``OutboxDeadEventsPresent`` and ``OutboxEventsQueuedRelayNotRunning`` would
    go blind.
    """
    apply_operator_tenant_transaction_scope(db.connection())
    row = db.execute(_BACKLOG).one()
    return OutboxBacklog(
        pending=int(row.pending),
        claimed=int(row.claimed),
        dead=int(row.dead),
        oldest_pending_age_seconds=max(0.0, float(row.oldest_age)),
    )
