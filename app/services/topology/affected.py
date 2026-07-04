"""Reverse traversal: infrastructure -> affected customers (Phase 4a).

The mirror of resolve_customer_path. Given a failing node or basestation,
enumerate the active subscriptions downstream of it — the engine for outage
impact assessment. Read-only; manual use (no auto-detection). An upstream
node expands to its downstream access nodes via the LLDP graph (moving away
from core), so an aggregation/core failure captures everything below it; with
no usable graph it degrades safely to the node itself.
"""

from __future__ import annotations

from collections import deque

from sqlalchemy import or_
from sqlalchemy.orm import Session

from app.models.catalog import Subscription, SubscriptionStatus
from app.models.network import (
    FdhCabinet,
    OntAssignment,
    OntUnit,
    Splitter,
    SplitterPort,
    SplitterPortAssignment,
)
from app.models.network_monitoring import (
    DeviceRole,
    NetworkDevice,
    NetworkTopologyLink,
    PopSite,
)
from app.services.topology.lldp_poller import SOURCE as LLDP_SOURCE


def list_basestations(session: Session) -> list[PopSite]:
    """Zabbix-linked pop_sites (basestations) for the outage pickers."""
    return (
        session.query(PopSite)
        .filter(PopSite.zabbix_group_id.isnot(None))
        .order_by(PopSite.name)
        .all()
    )


def list_fdh_cabinets(session: Session) -> list[FdhCabinet]:
    """Active FDH cabinets for outage-impact pickers."""
    return (
        session.query(FdhCabinet)
        .filter(FdhCabinet.is_active.is_(True))
        .order_by(FdhCabinet.name)
        .all()
    )


def _neighbor_ids(session: Session, node_id) -> list:
    links = (
        session.query(NetworkTopologyLink)
        .filter(
            NetworkTopologyLink.source == LLDP_SOURCE,
            NetworkTopologyLink.is_active.is_(True),
            or_(
                NetworkTopologyLink.source_device_id == node_id,
                NetworkTopologyLink.target_device_id == node_id,
            ),
        )
        .all()
    )
    return [
        link.target_device_id
        if link.source_device_id == node_id
        else link.source_device_id
        for link in links
    ]


def _dist_to_core(session: Session) -> dict:
    """BFS hop-distance to the nearest core node over the LLDP graph."""
    cores = (
        session.query(NetworkDevice)
        .filter(
            NetworkDevice.role == DeviceRole.core, NetworkDevice.is_active.is_(True)
        )
        .all()
    )
    dist: dict = {c.id: 0 for c in cores}
    queue: deque = deque((c.id, 0) for c in cores)
    while queue:
        nid, d = queue.popleft()
        for nb in _neighbor_ids(session, nid):
            if nb not in dist:
                dist[nb] = d + 1
                queue.append((nb, d + 1))
    return dist


def downstream_nodes(
    session: Session, root: NetworkDevice, *, dist: dict | None = None
) -> set:
    """Node ids at/below ``root`` — root plus nodes reachable moving strictly
    away from core (increasing distance-to-core). Degrades to {root} when the
    graph/core is unknown, so we never over-scope an outage.

    ``dist`` is the (root-independent) distance-to-core map; callers that invoke
    this repeatedly in one request can compute it once via ``_dist_to_core`` and
    pass it in to avoid a full-graph BFS per call.
    """
    if dist is None:
        dist = _dist_to_core(session)
    result = {root.id}
    queue: deque = deque([root.id])
    while queue:
        nid = queue.popleft()
        cur_d = dist.get(nid)
        for nb in _neighbor_ids(session, nid):
            if nb in result:
                continue
            nb_d = dist.get(nb)
            if cur_d is None or nb_d is None:
                continue  # unorderable -> don't expand (avoid over-scoping)
            if nb_d > cur_d:
                result.add(nb)
                queue.append(nb)
    return result


def subscriptions_for_node(session: Session, node: NetworkDevice) -> list[Subscription]:
    """Active subscriptions whose access path terminates at this node."""
    if node.matched_device_id is None:
        return []
    if node.matched_device_type == "nas":
        return (
            session.query(Subscription)
            .filter(
                Subscription.provisioning_nas_device_id == node.matched_device_id,
                Subscription.status == SubscriptionStatus.active,
            )
            .all()
        )
    if node.matched_device_type == "olt":
        ont_ids = [
            r[0]
            for r in session.query(OntUnit.id)
            .filter(OntUnit.olt_device_id == node.matched_device_id)
            .all()
        ]
        if not ont_ids:
            return []
        subscriber_ids = [
            r[0]
            for r in session.query(OntAssignment.subscriber_id)
            .filter(
                OntAssignment.ont_unit_id.in_(ont_ids),
                OntAssignment.active.is_(True),
                OntAssignment.subscriber_id.isnot(None),
            )
            .all()
        ]
        if not subscriber_ids:
            return []
        return (
            session.query(Subscription)
            .filter(
                Subscription.subscriber_id.in_(subscriber_ids),
                Subscription.status == SubscriptionStatus.active,
            )
            .all()
        )
    return []


def subscriptions_for_fdh(session: Session, fdh: FdhCabinet) -> list[Subscription]:
    """Active subscriptions downstream of an FDH cabinet.

    Uses explicit splitter-port assignments when available, plus direct ONT
    splitter references for imported/legacy plant data.
    """
    splitter_ids = [
        row[0]
        for row in session.query(Splitter.id)
        .filter(Splitter.fdh_id == fdh.id, Splitter.is_active.is_(True))
        .all()
    ]
    if not splitter_ids:
        return []

    splitter_port_ids = [
        row[0]
        for row in session.query(SplitterPort.id)
        .filter(
            SplitterPort.splitter_id.in_(splitter_ids),
            SplitterPort.is_active.is_(True),
        )
        .all()
    ]

    subscriber_ids: set = set()
    service_address_ids: set = set()
    if splitter_port_ids:
        for subscriber_id, service_address_id in (
            session.query(
                SplitterPortAssignment.subscriber_id,
                SplitterPortAssignment.service_address_id,
            )
            .filter(
                SplitterPortAssignment.splitter_port_id.in_(splitter_port_ids),
                SplitterPortAssignment.active.is_(True),
            )
            .all()
        ):
            if subscriber_id is not None:
                subscriber_ids.add(subscriber_id)
            if service_address_id is not None:
                service_address_ids.add(service_address_id)

        ont_rows = (
            session.query(OntAssignment.subscriber_id)
            .join(OntUnit, OntUnit.id == OntAssignment.ont_unit_id)
            .filter(
                OntAssignment.active.is_(True),
                OntAssignment.subscriber_id.isnot(None),
                OntUnit.splitter_port_id.in_(splitter_port_ids),
            )
            .all()
        )
        subscriber_ids.update(row[0] for row in ont_rows if row[0] is not None)

    ont_rows = (
        session.query(OntAssignment.subscriber_id)
        .join(OntUnit, OntUnit.id == OntAssignment.ont_unit_id)
        .filter(
            OntAssignment.active.is_(True),
            OntAssignment.subscriber_id.isnot(None),
            OntUnit.splitter_id.in_(splitter_ids),
        )
        .all()
    )
    subscriber_ids.update(row[0] for row in ont_rows if row[0] is not None)

    if not subscriber_ids and not service_address_ids:
        return []

    query = session.query(Subscription).filter(
        Subscription.status == SubscriptionStatus.active
    )
    filters = []
    if subscriber_ids:
        filters.append(Subscription.subscriber_id.in_(subscriber_ids))
    if service_address_ids:
        filters.append(Subscription.service_address_id.in_(service_address_ids))
    return query.filter(or_(*filters)).all()


def affected_customers(
    session: Session,
    node: NetworkDevice | None = None,
    basestation: PopSite | None = None,
    fdh: FdhCabinet | None = None,
) -> dict:
    """Subscriptions affected by a failing node and/or basestation.

    Returns {subscriptions, node_ids, count} (deduped). For a basestation, all
    its active nodes; for a node, it + its downstream access nodes.
    """
    node_ids: set = set()
    if basestation is not None:
        node_ids |= {
            n.id
            for n in session.query(NetworkDevice)
            .filter(
                NetworkDevice.pop_site_id == basestation.id,
                NetworkDevice.is_active.is_(True),
            )
            .all()
        }
    if node is not None:
        node_ids |= downstream_nodes(session, node)

    subs: dict = {}
    if fdh is not None:
        for s in subscriptions_for_fdh(session, fdh):
            subs[s.id] = s
    for nid in node_ids:
        n = session.get(NetworkDevice, nid)
        if n is None:
            continue
        for s in subscriptions_for_node(session, n):
            subs[s.id] = s
    return {
        "subscriptions": list(subs.values()),
        "node_ids": node_ids,
        "count": len(subs),
    }
