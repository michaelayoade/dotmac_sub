"""Manual outage management (Phase 4b).

An operator declares an outage against a node or basestation; the affected
subscriber count is snapshotted from affected_customers at declare time.
Manual only — no auto-detection, no notification sending here.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from sqlalchemy.orm import Session

from app.models.network import FdhCabinet, OntUnit, Splitter, SplitterPort
from app.models.network_monitoring import NetworkDevice, OutageIncident, PopSite
from app.services.topology.affected import (
    _dist_to_core,
    affected_customers,
    downstream_nodes,
)

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
    fdh: FdhCabinet | None = None,
    declared_by: str | None = None,
    note: str | None = None,
    severity: str | None = None,
) -> OutageIncident:
    """Open an incident against a target, snapshotting affected_count."""
    if node is None and basestation is None and fdh is None:
        raise ValueError("declare_outage requires a node, basestation, or FDH")
    impact = affected_customers(session, node=node, basestation=basestation, fdh=fdh)
    incident = OutageIncident(
        root_node_id=node.id if node is not None else None,
        basestation_id=basestation.id if basestation is not None else None,
        fdh_cabinet_id=fdh.id if fdh is not None else None,
        declared_by=declared_by,
        note=note,
        severity=severity,
        affected_count=impact["count"],
        status="open",
    )
    session.add(incident)
    session.flush()
    return incident


def _fdh_id_for_ont(session: Session, ont: OntUnit | None):
    if ont is None:
        return None
    if ont.splitter_id is not None:
        splitter = session.get(Splitter, ont.splitter_id)
        if splitter is not None and splitter.fdh_id is not None:
            return splitter.fdh_id
    if ont.splitter_port_id is not None:
        port = session.get(SplitterPort, ont.splitter_port_id)
        if port is not None:
            splitter = session.get(Splitter, port.splitter_id)
            if splitter is not None and splitter.fdh_id is not None:
                return splitter.fdh_id
    return None


def resolve_outage(session: Session, incident_id) -> OutageIncident | None:
    incident = session.get(OutageIncident, incident_id)
    if incident is None:
        return None
    if set_outage_status(incident, "resolved"):
        session.flush()
    return incident


def open_incident_for_path(session: Session, path) -> OutageIncident | None:
    """The open incident covering a customer's path, if any — matched on the
    access node, any upstream hop, the basestation, or by sitting within an
    incident's blast radius. ``path`` is a CustomerPath (duck-typed to avoid a
    circular import)."""
    if path is None:
        return None
    customer_node_ids = set()
    node = getattr(path, "node", None)
    customer_access_id = node.id if node is not None else None
    if customer_access_id is not None:
        customer_node_ids.add(customer_access_id)
    for hop in getattr(path, "upstream_chain", None) or []:
        customer_node_ids.add(hop.id)
    basestation = getattr(path, "basestation", None)
    basestation_id = basestation.id if basestation is not None else None
    fdh_id = _fdh_id_for_ont(session, getattr(path, "ont", None))

    incidents = (
        session.query(OutageIncident)
        .filter(OutageIncident.status == "open")
        .order_by(OutageIncident.started_at.desc())
        .all()
    )
    # Pass 1 (cheap): basestation match, or the incident root is on the
    # customer's own (hop-capped) path.
    for incident in incidents:
        if basestation_id is not None and incident.basestation_id == basestation_id:
            return incident
        if fdh_id is not None and incident.fdh_cabinet_id == fdh_id:
            return incident
        if (
            incident.root_node_id is not None
            and incident.root_node_id in customer_node_ids
        ):
            return incident
    # Pass 2 (blast radius): the customer is downstream of an incident root that
    # lies beyond their hop-capped upstream chain. This keeps the read-side
    # membership in sync with the declare-side affected_count (both computed via
    # downstream_nodes), so a counted customer always sees the banner. Only
    # reached during an active outage that didn't already match cheaply.
    root_incidents = [i for i in incidents if i.root_node_id is not None]
    if customer_access_id is not None and root_incidents:
        # _dist_to_core is root-independent; compute the full-graph BFS ONCE and
        # reuse it across incidents rather than recomputing inside each
        # downstream_nodes call (this runs on the customer connection-status
        # request path, possibly with many open incidents during a wide outage).
        dist = _dist_to_core(session)
        for incident in root_incidents:
            root = session.get(NetworkDevice, incident.root_node_id)
            if root is not None and customer_access_id in downstream_nodes(
                session, root, dist=dist
            ):
                return incident
    return None


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
