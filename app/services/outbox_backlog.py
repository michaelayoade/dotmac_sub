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


_TENANTS = text("SELECT id FROM public.tenants")
_SCOPE = text("SELECT set_config('app.current_tenant', :tenant, true)")


def read_outbox_backlog(db: Session) -> OutboxBacklog:
    """Sum the backlog across every tenant.

    ``outbox_events`` is FORCE-RLS, so the runtime role sees only the row set of
    the ``app.current_tenant`` in force. A single unscoped read would therefore
    report the operator tenant only, or zero. The tenant context is set per
    tenant (transaction-local, discarded by the caller's rollback) and the
    counts are summed. No grant or function is added.
    """
    pending = claimed = dead = 0
    oldest = 0.0
    tenant_ids = list(db.execute(_TENANTS).scalars())
    scopes: list[str | None] = [str(t) for t in tenant_ids] or [None]
    for tenant in scopes:
        if tenant is not None:
            db.execute(_SCOPE, {"tenant": tenant})
        row = db.execute(_BACKLOG).one()
        pending += int(row.pending)
        claimed += int(row.claimed)
        dead += int(row.dead)
        oldest = max(oldest, float(row.oldest_age))
    return OutboxBacklog(
        pending=pending,
        claimed=claimed,
        dead=dead,
        oldest_pending_age_seconds=max(0.0, oldest),
    )
