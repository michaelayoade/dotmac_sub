"""Manual outage management (Phase 4b).

An operator declares an outage against a node or basestation; the affected
subscriber count is snapshotted from affected_customers at declare time.
Manual only — no auto-detection, no notification sending here.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from sqlalchemy import or_
from sqlalchemy.orm import Session

from app.models.network_monitoring import NetworkDevice, OutageIncident, PopSite
from app.services.topology.affected import affected_customers

# status is a free-form String column; these are the only legal values.
_OUTAGE_STATUSES = frozenset({"open", "resolved"})
# An open incident older than this is surfaced for operator review — a lingering
# open incident keeps showing customers a false "known outage" banner. Manual
# only: it is flagged, never auto-resolved (auto-resolve would mis-fire on a
# flapping link).
STALE_OPEN_HOURS = 36


def set_outage_status(incident: OutageIncident, status: str) -> bool:
    """Guarded status writer. Returns True if it changed. Idempotent; stamps
    resolved_at on the open->resolved transition only."""
    if status not in _OUTAGE_STATUSES:
        raise ValueError(f"invalid outage status: {status!r}")
    if incident.status == status:
        return False
    incident.status = status
    incident.resolved_at = datetime.now(UTC) if status == "resolved" else None
    return True


def is_stale_open(incident: OutageIncident, *, now: datetime | None = None) -> bool:
    """True for an `open` incident that has lingered past STALE_OPEN_HOURS."""
    if incident.status != "open" or incident.started_at is None:
        return False
    started = incident.started_at
    if started.tzinfo is None:
        started = started.replace(tzinfo=UTC)
    now = now or datetime.now(UTC)
    return (now - started) >= timedelta(hours=STALE_OPEN_HOURS)


def declare_outage(
    session: Session,
    *,
    node: NetworkDevice | None = None,
    basestation: PopSite | None = None,
    declared_by: str | None = None,
    note: str | None = None,
    severity: str | None = None,
) -> OutageIncident:
    """Open an incident against a node/basestation, snapshotting affected_count."""
    if node is None and basestation is None:
        raise ValueError("declare_outage requires a node or a basestation")
    impact = affected_customers(session, node=node, basestation=basestation)
    incident = OutageIncident(
        root_node_id=node.id if node is not None else None,
        basestation_id=basestation.id if basestation is not None else None,
        declared_by=declared_by,
        note=note,
        severity=severity,
        affected_count=impact["count"],
        status="open",
    )
    session.add(incident)
    session.flush()
    return incident


def resolve_outage(session: Session, incident_id) -> OutageIncident | None:
    incident = session.get(OutageIncident, incident_id)
    if incident is None:
        return None
    if set_outage_status(incident, "resolved"):
        session.flush()
    return incident


def open_incident_for_path(session: Session, path) -> OutageIncident | None:
    """The open incident covering a customer's path, if any — matched on the
    access node, any upstream hop, or the basestation. ``path`` is a
    CustomerPath (duck-typed to avoid a circular import)."""
    if path is None:
        return None
    node_ids = set()
    if getattr(path, "node", None) is not None:
        node_ids.add(path.node.id)
    for hop in getattr(path, "upstream_chain", None) or []:
        node_ids.add(hop.id)
    conds = []
    if node_ids:
        conds.append(OutageIncident.root_node_id.in_(node_ids))
    if getattr(path, "basestation", None) is not None:
        conds.append(OutageIncident.basestation_id == path.basestation.id)
    if not conds:
        return None
    return (
        session.query(OutageIncident)
        .filter(OutageIncident.status == "open", or_(*conds))
        .order_by(OutageIncident.started_at.desc())
        .first()
    )


def list_open_incidents(session: Session) -> list[OutageIncident]:
    return (
        session.query(OutageIncident)
        .filter(OutageIncident.status == "open")
        .order_by(OutageIncident.started_at.desc())
        .all()
    )


def list_stale_open_incidents(
    session: Session, *, older_than_hours: int = STALE_OPEN_HOURS
) -> list[OutageIncident]:
    """Open incidents that have lingered past the threshold — likely forgotten,
    still showing customers a false outage banner. For operator review only."""
    cutoff = datetime.now(UTC) - timedelta(hours=older_than_hours)
    return (
        session.query(OutageIncident)
        .filter(OutageIncident.status == "open", OutageIncident.started_at < cutoff)
        .order_by(OutageIncident.started_at.asc())
        .all()
    )
