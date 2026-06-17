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
from app.models.network import OntAssignment, OntUnit
from app.models.network_monitoring import (
    DeviceRole,
    NetworkDevice,
    NetworkTopologyLink,
    PopSite,
)
from app.services.topology.lldp_poller import SOURCE as LLDP_SOURCE


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


def downstream_nodes(session: Session, root: NetworkDevice) -> set:
    """Node ids at/below ``root`` — root plus nodes reachable moving strictly
    away from core (increasing distance-to-core). Degrades to {root} when the
    graph/core is unknown, so we never over-scope an outage."""
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


def affected_customers(
    session: Session,
    node: NetworkDevice | None = None,
    basestation: PopSite | None = None,
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
