"""Outage console read-model — P4 surface (design §P4, docs/designs/OUTAGE_CLASSIFIER.md).

The classifier-driven counterpart to the *manual* outage console: instead of an
operator declaring an incident against infrastructure, this reads the P1/P2/P3
engines and shows what is *actually* down right now, how deep, and why.

  - ``network_health_summary`` — run P1 ``classify_node`` across every pingable
    infra node (OLT / NAS / AP / BTS-router); counts by class + the not-healthy
    nodes with their affected/online figures and (for faults) the localized
    boundary. The console's top-level feed.
  - ``active_outages`` — just the localized failure boundaries (deepest
    dark-under-live, P1 ``localize_outage``), deduped, with a SEPARATE
    monitoring-fault bucket: a session-up-while-ping-down contradiction is a
    broken check to self-heal, NOT an outage (design §1).
  - ``outage_detail`` — one domain drill-in: each session-down customer's P2
    ``diagnose_last_mile`` verdict, plus P3 predictive branch alerts
    (``infer_branches`` / ``detect_rx_droop``) when the node is an OLT.

Pure read service. The fleet sweep batches topology, subscriptions, and live
sessions; the drill-in reuses ``affected_customers`` / ``diagnose_many``.
Large sweeps are capped and logged rather than silently truncated. Nothing
here writes.
"""

from __future__ import annotations

import logging
import uuid
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
from typing import NotRequired, TypedDict

from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session

from app.models.network import FdhCabinet, OntUnit
from app.models.network_monitoring import (
    CustomerOutageInterval,
    DeviceRole,
    DeviceType,
    NetworkDevice,
    OutageIncident,
    PopSite,
)
from app.models.support import Ticket
from app.models.usage import RadiusAccountingSession
from app.services.customer_service_state import (
    OPEN_INFRASTRUCTURE_TICKET_STATUSES,
    is_infrastructure_down_ticket,
)
from app.services.customer_support_links import ticket_customer_linked_ids
from app.services.device_operational_status import warmer_is_stale
from app.services.topology.affected import (
    _dist_to_core,
    affected_customers,
    downstream_nodes,
    forwarding_graph_projection,
    subscriptions_for_nodes,
)
from app.services.topology.health_classifier import (
    HEALTHY,
    MONITORING_FAULT,
    NODE_OUTAGE,
    SERVICE_FAULT,
    UNKNOWN,
    LocalizationSnapshot,
    OutageBoundary,
    classify_node,
    localize_outage,
    online_subscription_ids,
)
from app.services.topology.last_mile import diagnose_many
from app.services.topology.splice_inference import detect_rx_droop, infer_branches

logger = logging.getLogger(__name__)

NodeBrief = TypedDict(
    "NodeBrief",
    {
        "node_id": uuid.UUID,
        "name": str,
        "role": str | None,
        "medium": str,
        "live_status": str | None,
        "class": str,
        "count": int,
        "online_count": int,
        "boundary": NotRequired[OutageBoundary | None],
    },
)


class NetworkHealthSummary(TypedDict):
    counts: dict[str, int]
    total_nodes: int
    not_healthy: list[NodeBrief]


class ClassifierIncidentLocation(TypedDict):
    node_name: str | None
    basestation_name: str | None


def classifier_incident_locations(
    session: Session, incidents: Sequence[OutageIncident]
) -> dict[uuid.UUID, ClassifierIncidentLocation]:
    """Resolve incident display names in two bounded inventory queries."""
    node_ids = {inc.root_node_id for inc in incidents if inc.root_node_id is not None}
    nodes = (
        {
            node.id: node
            for node in session.query(NetworkDevice)
            .filter(NetworkDevice.id.in_(node_ids))
            .all()
        }
        if node_ids
        else {}
    )
    pop_ids = {
        inc.basestation_id for inc in incidents if inc.basestation_id is not None
    }
    pop_ids.update(
        node.pop_site_id for node in nodes.values() if node.pop_site_id is not None
    )
    pops = (
        {
            pop.id: pop
            for pop in session.query(PopSite).filter(PopSite.id.in_(pop_ids)).all()
        }
        if pop_ids
        else {}
    )
    result: dict[uuid.UUID, ClassifierIncidentLocation] = {}
    for inc in incidents:
        node = nodes.get(inc.root_node_id) if inc.root_node_id is not None else None
        pop_id = inc.basestation_id or (node.pop_site_id if node else None)
        basestation = pops.get(pop_id) if pop_id is not None else None
        result[inc.id] = {
            "node_name": node.name if node is not None else None,
            "basestation_name": (basestation.name if basestation is not None else None),
        }
    return result


ActiveOutageRow = TypedDict(
    "ActiveOutageRow",
    {
        "failure_node": uuid.UUID,
        "name": str,
        "class": str,
        "medium": str,
        "affected": int,
        "online_now": int,
        "confidence": str,
        "localized": bool,
    },
)


class MonitoringFaultRow(TypedDict):
    node_id: uuid.UUID
    name: str
    online_count: int
    live_status: str | None
    note: str


class ActiveOutages(TypedDict):
    outages: list[ActiveOutageRow]
    monitoring_faults: list[MonitoringFaultRow]


class CoFailureBranchAlert(TypedDict):
    pon_port_id: uuid.UUID
    ont_unit_ids: list[uuid.UUID]
    support: int
    confidence: str


class RxDroopBranchAlert(TypedDict):
    pon_port_id: uuid.UUID
    ont_unit_ids: list[uuid.UUID]
    shared_shift_db: float
    confidence: str


class PredictiveBranches(TypedDict):
    co_failure: list[CoFailureBranchAlert]
    rx_droop: list[RxDroopBranchAlert]


class PredictiveBranchesPage(PredictiveBranches):
    page: int
    has_next: bool
    total_ports: int


class DomainNode(TypedDict):
    id: uuid.UUID
    name: str
    role: str | None
    medium: str
    live_status: str | None


class CustomerDiagnosis(TypedDict):
    subscription_id: uuid.UUID
    subscriber_name: str
    online: bool
    verdict: str | None
    medium: str | None
    signal_dbm: float | None
    customer_message: str | None
    agent_action: str | None
    evidence: Mapping[str, object] | None


OutageDetail = TypedDict(
    "OutageDetail",
    {
        "node": DomainNode,
        "class": str,
        "count": int,
        "online_count": int,
        "boundary": OutageBoundary | None,
        "customers": list[CustomerDiagnosis],
        "capped": bool,
        "customer_offset": int,
        "total_customers": int,
        "has_next": bool,
        "predictive": PredictiveBranches,
    },
)


# Infra roles we ping/classify — everything that is a shared node. CPE is the
# customer's own device (P2 territory), never a plant node, so it's excluded.
_INFRA_ROLES = frozenset(
    {
        DeviceRole.core,
        DeviceRole.distribution,
        DeviceRole.access,
        DeviceRole.aggregation,
        DeviceRole.edge,
    }
)

# Bound the live sweep. Real fleets are in the low hundreds of infra
# nodes; beyond this we cap and LOG rather than melt the request (design: no
# silent truncation). Raise if the fleet outgrows it.
MAX_NODES = 750

# Cap per-domain last-mile diagnosis on the drill-in — a huge domain would fan
# out N× resolve_customer_path. Log when we clip.
MAX_DETAIL_CUSTOMERS = 300

# Classes that represent a real, actionable fault (vs healthy / unknown /
# monitoring_fault which are handled separately).
_FAULT_CLASSES = frozenset({NODE_OUTAGE, SERVICE_FAULT})

_AREA_OUTAGE_VERDICT = "area_outage"
_NETWORK_MEDIUM = "network"


def _pingable_infra_nodes(session: Session) -> list[NetworkDevice]:
    rows = (
        session.query(NetworkDevice)
        .filter(
            NetworkDevice.is_active.is_(True),
            NetworkDevice.ping_enabled.is_(True),
            NetworkDevice.role.in_(_INFRA_ROLES),
        )
        .order_by(NetworkDevice.name)
        .limit(MAX_NODES + 1)
        .all()
    )
    if len(rows) > MAX_NODES:
        logger.warning(
            "outage_console: %d pingable infra nodes exceeds cap %d — "
            "classifying the first %d by name only",
            len(rows),
            MAX_NODES,
            MAX_NODES,
        )
        rows = rows[:MAX_NODES]
    return rows


def _node_medium(node: NetworkDevice) -> str:
    """Coarse access medium for a node (design §4 asymmetry).

    Fiber if it's an OLT; wireless if it's an access point / BTS radio; else
    network for a shared NAS/router node; otherwise unknown. Only used for
    display + the fiber/wireless precision note.
    """
    if node.matched_device_type == "olt":
        return "fiber"
    if node.device_type == DeviceType.access_point:
        return "wireless"
    if node.matched_device_type == "nas" or node.device_type == DeviceType.router:
        return _NETWORK_MEDIUM
    return "unknown"


def _customer_display_verdict(node: NetworkDevice, cls: str, verdict: dict) -> dict:
    """Operator-facing customer verdict for an outage-domain table row.

    P2 last-mile diagnosis is intentionally conservative: a NAS-only affected
    subscription has no ONT/radio telemetry below the PPPoE session, so it
    returns unknown. In a drill-in for a confirmed shared-node outage, the
    operator-facing answer is still known: the customer is affected by the
    selected network node being down.
    """
    evidence = verdict.get("evidence") or {}
    if (
        cls == NODE_OUTAGE
        and verdict.get("verdict") == UNKNOWN
        and evidence.get("access_device_kind") == "nas"
    ):
        display = dict(verdict)
        display["verdict"] = _AREA_OUTAGE_VERDICT
        display["medium"] = _NETWORK_MEDIUM
        display["customer_message"] = (
            f"Affected by shared network outage at {node.name}."
        )
        display["agent_action"] = f"network_team_restore_node - restore {node.name}"
        display["evidence"] = {
            **evidence,
            "raw_verdict": verdict.get("verdict"),
            "display_override": "shared_node_outage",
        }
        return display
    return verdict


def _node_brief(node: NetworkDevice, impact: dict, cls: str) -> NodeBrief:
    return {
        "node_id": node.id,
        "name": node.name,
        "role": node.role.value if node.role else None,
        "medium": _node_medium(node),
        "live_status": node.live_status,
        "class": cls,
        "count": impact["count"],
        "online_count": impact["online_count"],
    }


def network_health_summary(
    session: Session, *, now: datetime | None = None
) -> NetworkHealthSummary:
    """Classify every pingable infra node; counts by class + the not-healthy set.

    Returns::

        {
          "counts": {healthy, service_fault, node_outage, monitoring_fault,
                     unknown},
          "total_nodes": int,
          "not_healthy": [ {node_id, name, role, medium, live_status, class,
                            count, online_count, boundary?}, ... ],
        }

    ``boundary`` (present only for fault classes) is the P1 ``localize_outage``
    result for that node's domain — the deepest dark-under-live element.
    """
    nodes = _pingable_infra_nodes(session)
    counts = {
        HEALTHY: 0,
        SERVICE_FAULT: 0,
        NODE_OUTAGE: 0,
        MONITORING_FAULT: 0,
        UNKNOWN: 0,
    }
    not_healthy: list[NodeBrief] = []

    if not nodes:
        return {"counts": counts, "total_nodes": 0, "not_healthy": []}

    now = now or datetime.now(UTC)
    graph = forwarding_graph_projection(session)
    dist = _dist_to_core(
        session, adjacency=graph.adjacency, root_ids=graph.root_device_ids
    )
    scopes = {
        node.id: downstream_nodes(session, node, dist=dist, adjacency=graph.adjacency)
        for node in nodes
    }
    scope_ids = set().union(*scopes.values())
    by_node = subscriptions_for_nodes(session, scope_ids)
    online_ids = online_subscription_ids(
        session, {sub.id for subs in by_node.values() for sub in subs}, now=now
    )
    snapshot = LocalizationSnapshot(
        provisioned_by_node={nid: len(by_node.get(nid, ())) for nid in scope_ids},
        online_by_node={
            nid: sum(sub.id in online_ids for sub in by_node.get(nid, ()))
            for nid in scope_ids
        },
        distance_to_core=dist,
    )
    warm_stale = warmer_is_stale(now)
    for node in nodes:
        subscription_ids = {
            sub.id for nid in scopes[node.id] for sub in by_node.get(nid, ())
        }
        impact = {
            "count": len(subscription_ids),
            "online_count": len(subscription_ids & online_ids),
        }
        cls = classify_node(
            node,
            impact["online_count"],
            had_prior_life=impact["count"] > 0,
            now=now,
            warm_stale=warm_stale,
        )
        counts[cls] = counts.get(cls, 0) + 1
        if cls == HEALTHY:
            continue
        brief = _node_brief(node, impact, cls)
        if cls in _FAULT_CLASSES:
            brief["boundary"] = localize_outage(
                session,
                scopes[node.id],
                now=now,
                warm_stale=warm_stale,
                snapshot=snapshot,
            )
        not_healthy.append(brief)

    # Surface the worst first: outages, then service faults, then the rest.
    _order = {NODE_OUTAGE: 0, SERVICE_FAULT: 1, MONITORING_FAULT: 2, UNKNOWN: 3}
    not_healthy.sort(key=lambda b: (_order.get(b["class"], 9), -b["count"]))

    return {
        "counts": counts,
        "total_nodes": len(nodes),
        "not_healthy": not_healthy,
    }


def active_outages(
    session: Session,
    *,
    now: datetime | None = None,
    summary: NetworkHealthSummary | None = None,
) -> ActiveOutages:
    """Localized failure boundaries + the self-heal (monitoring-fault) queue.

    ``outages`` are deduped by boundary node (several parents can localize to
    the same deepest element). ``monitoring_faults`` are the impossible
    contradictions — session up while ping/snmp says down — which are NOT
    outages but broken checks to self-heal (design §1).

    Returns ``{outages: [...], monitoring_faults: [...]}``.
    """
    if summary is None:
        summary = network_health_summary(session, now=now)

    outages_by_boundary: dict[uuid.UUID, ActiveOutageRow] = {}
    monitoring_faults: list[MonitoringFaultRow] = []

    for brief in summary["not_healthy"]:
        if brief["class"] == MONITORING_FAULT:
            monitoring_faults.append(
                {
                    "node_id": brief["node_id"],
                    "name": brief["name"],
                    "online_count": brief["online_count"],
                    "live_status": brief["live_status"],
                    "note": "session up while ping/snmp reports down — "
                    "self-heal the check, not an outage",
                }
            )
            continue
        if brief["class"] not in _FAULT_CLASSES:
            continue
        boundary = brief.get("boundary")
        # No boundary means every element with customers still has a survivor —
        # a service_fault with nothing localized. Anchor it on the node itself
        # so the operator still sees it.
        bnode = boundary["failure_node"] if boundary else brief["node_id"]
        existing = outages_by_boundary.get(bnode)
        row: ActiveOutageRow = {
            "failure_node": bnode,
            "name": brief["name"],
            "class": (boundary["class"] if boundary else brief["class"]),
            "medium": brief["medium"],
            "affected": (
                boundary["affected_online_before"] if boundary else brief["count"]
            ),
            "online_now": brief["online_count"],
            "confidence": boundary["confidence"] if boundary else "low",
            "localized": boundary is not None,
        }
        # Keep the row with the larger affected footprint if two collide.
        if existing is None or row["affected"] > existing["affected"]:
            outages_by_boundary[bnode] = row

    outages = sorted(outages_by_boundary.values(), key=lambda r: -r["affected"])
    return {"outages": outages, "monitoring_faults": monitoring_faults}


def _olt_pon_port_ids(session: Session, olt_device_id: uuid.UUID) -> list[uuid.UUID]:
    """Distinct PON ports carrying ONTs on this OLT (P3 keys on ont pon_port)."""
    rows = session.execute(
        select(OntUnit.pon_port_id)
        .where(
            OntUnit.olt_device_id == olt_device_id,
            OntUnit.pon_port_id.is_not(None),
        )
        .distinct()
        .order_by(OntUnit.pon_port_id)
    ).all()
    return [r[0] for r in rows if r[0] is not None]


def _predictive_branches(
    session: Session,
    node: NetworkDevice,
    *,
    now: datetime | None,
    pon_ids: list[uuid.UUID] | None = None,
) -> PredictiveBranches:
    """P3 branch alerts for an OLT node — co-failure + correlated-Rx droop.

    Empty for non-OLT nodes (wireless localizes only to the BTS — design §4).
    """
    if node.matched_device_type != "olt" or node.matched_device_id is None:
        return {"co_failure": [], "rx_droop": []}
    co_failure: list[CoFailureBranchAlert] = []
    rx_droop: list[RxDroopBranchAlert] = []
    for pon_id in (
        pon_ids
        if pon_ids is not None
        else _olt_pon_port_ids(session, node.matched_device_id)
    ):
        for b in infer_branches(session, pon_id, now=now):
            co_failure.append(
                {
                    "pon_port_id": pon_id,
                    "ont_unit_ids": b["ont_unit_ids"],
                    "support": b["support"],
                    "confidence": b["confidence"],
                }
            )
        for d in detect_rx_droop(session, pon_id, now=now):
            rx_droop.append(
                {
                    "pon_port_id": pon_id,
                    "ont_unit_ids": d["ont_unit_ids"],
                    "shared_shift_db": d["shared_shift_db"],
                    "confidence": d["confidence"],
                }
            )
    return {"co_failure": co_failure, "rx_droop": rx_droop}


def predictive_branches_page(
    session: Session, node_id: uuid.UUID, *, page: int = 1
) -> PredictiveBranchesPage | None:
    """Run P3 inference for one PON per request instead of an entire OLT."""
    node = session.get(NetworkDevice, node_id)
    if node is None:
        return None
    if node.matched_device_type != "olt" or node.matched_device_id is None:
        return {
            "co_failure": [],
            "rx_droop": [],
            "page": page,
            "has_next": False,
            "total_ports": 0,
        }
    pon_ids = _olt_pon_port_ids(session, node.matched_device_id)
    selected = pon_ids[page - 1 : page]
    branches = _predictive_branches(session, node, now=None, pon_ids=selected)
    return {
        "co_failure": branches["co_failure"],
        "rx_droop": branches["rx_droop"],
        "page": page,
        "has_next": page < len(pon_ids),
        "total_ports": len(pon_ids),
    }


def outage_detail(
    session: Session,
    node_id: uuid.UUID,
    *,
    now: datetime | None = None,
    customer_offset: int = 0,
    customer_limit: int = MAX_DETAIL_CUSTOMERS,
    include_predictive: bool = True,
) -> OutageDetail | None:
    """One domain drill-in: per-customer P2 verdicts + P3 predictive alerts.

    Returns ``None`` if the node doesn't exist. Otherwise::

        {
          "node": {id, name, role, medium, live_status},
          "class": <P1 class>,
          "count", "online_count",
          "boundary": <localize_outage or None>,
          "customers": [ {subscription_id, subscriber_name, online,
                          **diagnose_last_mile} ... ],   # session-down first
          "capped": bool,
          "predictive": {co_failure: [...], rx_droop: [...]},
        }
    """
    node = session.get(NetworkDevice, node_id)
    if node is None:
        return None

    impact = affected_customers(session, node=node)
    warm_stale = warmer_is_stale(now)
    cls = classify_node(
        node,
        impact["online_count"],
        had_prior_life=impact["count"] > 0,
        now=now,
        warm_stale=warm_stale,
    )

    subs = sorted(impact["subscriptions"], key=lambda sub: str(sub.id))
    total_customers = len(subs)
    if customer_limit > MAX_DETAIL_CUSTOMERS:
        customer_limit = MAX_DETAIL_CUSTOMERS
    capped = total_customers > customer_offset + customer_limit
    if capped:
        logger.warning(
            "outage_console: domain for node %s has %d customers; "
            "diagnosing offset %d, limit %d",
            node_id,
            total_customers,
            customer_offset,
            customer_limit,
        )
    subs = subs[customer_offset : customer_offset + customer_limit]

    verdicts = diagnose_many(session, [s.id for s in subs], now=now)
    customers: list[CustomerDiagnosis] = []
    for s in subs:
        v = _customer_display_verdict(node, cls, verdicts.get(s.id, {}))
        online = v.get("verdict") == "healthy"
        customers.append(
            {
                "subscription_id": s.id,
                "subscriber_name": (
                    f"{s.subscriber.first_name} {s.subscriber.last_name}"
                    if s.subscriber
                    else "—"
                ),
                "online": online,
                "verdict": v.get("verdict"),
                "medium": v.get("medium"),
                "signal_dbm": v.get("signal_dbm"),
                "customer_message": v.get("customer_message"),
                "agent_action": v.get("agent_action"),
                "evidence": v.get("evidence"),
            }
        )
    # Session-down (the actionable ones) first, then by verdict for grouping.
    customers.sort(key=lambda c: (c["online"], c["verdict"] or ""))

    result: OutageDetail = {
        "node": {
            "id": node.id,
            "name": node.name,
            "role": node.role.value if node.role else None,
            "medium": _node_medium(node),
            "live_status": node.live_status,
        },
        "class": cls,
        "count": impact["count"],
        "online_count": impact["online_count"],
        "boundary": localize_outage(
            session, impact["node_ids"], now=now, warm_stale=warm_stale
        ),
        "customers": customers,
        "capped": capped,
        "customer_offset": customer_offset,
        "total_customers": total_customers,
        "has_next": capped,
        "predictive": (
            _predictive_branches(session, node, now=now)
            if include_predictive
            else {"co_failure": [], "rx_droop": []}
        ),
    }
    return result


# Customer connectivity view for healthy or down infrastructure scopes.


class ConnectivityScope(StrEnum):
    basestation = "basestation"
    cabinet = "cabinet"
    node = "node"


class ConnectivityFilter(StrEnum):
    all = "all"
    no_session = "no_session"
    no_ticket = "no_ticket"


@dataclass(frozen=True, slots=True)
class ScopeChoice:
    id: uuid.UUID
    name: str


@dataclass(frozen=True, slots=True)
class ConnectivityRow:
    subscription_id: uuid.UUID
    customer_name: str
    has_live_session: bool
    verdict: str | None
    medium: str | None
    signal_dbm: float | None
    downtime_started_at: datetime | None
    downtime_seconds: int | None
    downtime_quality: str | None
    ticket_id: uuid.UUID | None
    ticket_number: str | None
    ticket_status: str | None


@dataclass(frozen=True, slots=True)
class ConnectivityPage:
    scope: ConnectivityScope
    scope_id: uuid.UUID
    scope_name: str
    equipment_status: str | None
    total_customers: int
    without_session: int
    open_service_tickets: int
    rows: tuple[ConnectivityRow, ...]
    page: int
    has_next: bool


def search_scopes(
    db: Session, *, scope: ConnectivityScope, query: str, limit: int = 25
) -> tuple[ScopeChoice, ...]:
    """Find active assets, including healthy ones, without a live fleet sweep."""
    model = {
        ConnectivityScope.basestation: PopSite,
        ConnectivityScope.cabinet: FdhCabinet,
        ConnectivityScope.node: NetworkDevice,
    }[scope]
    statement = select(model.id, model.name).where(model.is_active.is_(True))
    term = query.strip()
    if term:
        statement = statement.where(model.name.ilike(f"%{term}%"))
    statement = statement.order_by(model.name, model.id).limit(limit)
    return tuple(ScopeChoice(id=row.id, name=row.name) for row in db.execute(statement))


def customer_connectivity(
    db: Session,
    *,
    scope: ConnectivityScope,
    scope_id: uuid.UUID,
    status_filter: ConnectivityFilter = ConnectivityFilter.all,
    page: int = 1,
    page_size: int = 50,
    now: datetime | None = None,
    include_tickets: bool = False,
) -> ConnectivityPage | None:
    """All mapped customers in a BTS, cabinet, or node, regardless of health."""
    model = {
        ConnectivityScope.basestation: PopSite,
        ConnectivityScope.cabinet: FdhCabinet,
        ConnectivityScope.node: NetworkDevice,
    }[scope]
    asset = db.get(model, scope_id)
    if asset is None or not asset.is_active:
        return None
    page_size = max(1, min(page_size, 50))
    instant = now or datetime.now(UTC)
    impact = affected_customers(
        db,
        basestation=asset if scope == ConnectivityScope.basestation else None,
        fdh=asset if scope == ConnectivityScope.cabinet else None,
        node=asset if scope == ConnectivityScope.node else None,
    )
    subscriptions = sorted(impact["subscriptions"], key=lambda sub: str(sub.id))
    subscription_ids = {sub.id for sub in subscriptions}
    live_ids = online_subscription_ids(db, subscription_ids, now=instant)
    subscriber_ids = {sub.subscriber_id for sub in subscriptions if sub.subscriber_id}

    intervals: dict[uuid.UUID, CustomerOutageInterval] = {}
    if subscription_ids:
        for interval in db.scalars(
            select(CustomerOutageInterval)
            .where(
                CustomerOutageInterval.subscription_id.in_(subscription_ids),
                CustomerOutageInterval.ended_at.is_(None),
                CustomerOutageInterval.finalized_at.is_(None),
                CustomerOutageInterval.state == "confirmed_unavailable",
            )
            .order_by(CustomerOutageInterval.started_at)
        ):
            intervals.setdefault(interval.subscription_id, interval)

    # Imported accounting stops are useful for investigation, but they can
    # lag. Keep their timestamps explicitly estimated, never ledger-confirmed.
    last_disconnects: dict[uuid.UUID, datetime] = {}
    if subscription_ids:
        for subscription_id, ended_at in db.execute(
            select(
                RadiusAccountingSession.subscription_id,
                func.max(RadiusAccountingSession.session_end),
            )
            .where(
                RadiusAccountingSession.subscription_id.in_(subscription_ids),
                RadiusAccountingSession.session_end.is_not(None),
                RadiusAccountingSession.session_end <= instant,
            )
            .group_by(RadiusAccountingSession.subscription_id)
        ):
            if subscription_id is not None and ended_at is not None:
                last_disconnects[subscription_id] = ended_at

    tickets: dict[uuid.UUID, Ticket] = {}
    if include_tickets and subscriber_ids:
        rows = db.scalars(
            select(Ticket)
            .where(
                Ticket.is_active.is_(True),
                Ticket.merged_into_ticket_id.is_(None),
                Ticket.status.in_(OPEN_INFRASTRUCTURE_TICKET_STATUSES),
                or_(
                    Ticket.subscriber_id.in_(subscriber_ids),
                    Ticket.customer_account_id.in_(subscriber_ids),
                    Ticket.customer_person_id.in_(subscriber_ids),
                ),
            )
            .order_by(Ticket.created_at.desc(), Ticket.id)
        )
        for ticket in rows:
            if not is_infrastructure_down_ticket(ticket):
                continue
            for subscriber_id in ticket_customer_linked_ids(ticket):
                if subscriber_id in subscriber_ids:
                    tickets.setdefault(subscriber_id, ticket)

    selected = [
        sub
        for sub in subscriptions
        if (status_filter != ConnectivityFilter.no_session or sub.id not in live_ids)
        and (
            status_filter != ConnectivityFilter.no_ticket
            or (sub.id not in live_ids and sub.subscriber_id not in tickets)
        )
    ]
    selected.sort(key=lambda sub: (sub.id in live_ids, str(sub.id)))
    offset = (page - 1) * page_size
    visible = selected[offset : offset + page_size]
    verdicts = diagnose_many(db, [sub.id for sub in visible], now=instant)
    result_rows: list[ConnectivityRow] = []
    for sub in visible:
        diagnosis = verdicts.get(sub.id, {})
        interval = intervals.get(sub.id) if sub.id not in live_ids else None
        ticket = tickets.get(sub.subscriber_id)
        started = (
            interval.started_at
            if interval
            else last_disconnects.get(sub.id)
            if sub.id not in live_ids
            else None
        )
        if started is not None and started.tzinfo is None:
            started = started.replace(tzinfo=UTC)
        result_rows.append(
            ConnectivityRow(
                subscription_id=sub.id,
                customer_name=(
                    f"{sub.subscriber.first_name} {sub.subscriber.last_name}"
                    if sub.subscriber
                    else "Unknown customer"
                ),
                has_live_session=sub.id in live_ids,
                verdict=diagnosis.get("verdict"),
                medium=diagnosis.get("medium"),
                signal_dbm=diagnosis.get("signal_dbm"),
                downtime_started_at=started,
                downtime_seconds=(
                    max(0, int((instant - started).total_seconds()))
                    if started
                    else None
                ),
                downtime_quality=(
                    interval.quality
                    if interval
                    else "estimated_from_last_disconnect"
                    if started
                    else None
                ),
                ticket_id=ticket.id if ticket else None,
                ticket_number=ticket.number if ticket else None,
                ticket_status=ticket.status if ticket else None,
            )
        )
    return ConnectivityPage(
        scope=scope,
        scope_id=scope_id,
        scope_name=asset.name,
        equipment_status=getattr(asset, "live_status", None),
        total_customers=len(subscriptions),
        without_session=len(subscription_ids - live_ids),
        open_service_tickets=len(tickets),
        rows=tuple(result_rows),
        page=page,
        has_next=offset + page_size < len(selected),
    )
