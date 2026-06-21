"""Resolve a subscription's end-to-end path: ONT -> access device -> basestation.

Pure read; never calls Zabbix. Walks the provisioning edges sub already owns to
a NetworkDevice node (linked by the reconcile's matched_device_*), then to the
node's pop_site (the basestation). Returns a partial result + a gap marker when
the chain breaks, so support sees *where* provisioning is incomplete rather than
a blank panel.
"""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass, field
from typing import Any

from sqlalchemy import or_
from sqlalchemy.orm import Session

from app.models.catalog import NasDevice, Subscription
from app.models.network import OLTDevice, OntAssignment, OntUnit
from app.models.network_monitoring import (
    DeviceRole,
    NetworkDevice,
    NetworkTopologyLink,
    PopSite,
)
from app.services.topology.lldp_poller import SOURCE as LLDP_SOURCE

# Max hops to walk toward core (guards against pathological graphs).
_MAX_UPSTREAM_HOPS = 8

# Gap markers (None = complete path).
GAP_NO_ONT = "no_ont"  # no resolvable access device (provisioning incomplete)
GAP_NO_NODE = "no_node"  # device not matched to a topology node
GAP_NO_BASESTATION = "no_basestation"  # node not mapped to a basestation


@dataclass
class CustomerPath:
    ont: OntUnit | None = None
    access_device: Any | None = None  # OLTDevice | NasDevice
    access_device_kind: str | None = None  # 'olt' | 'nas'
    node: NetworkDevice | None = None
    basestation: PopSite | None = None
    # Device hops above the access node toward core (LLDP graph), [agg... core].
    # Empty when no core is reachable (graph not yet built / unmapped upstream).
    upstream_chain: list[NetworkDevice] = field(default_factory=list)
    gap: str | None = None


def _lldp_neighbor_ids(session: Session, node_id) -> list:
    """Adjacent node ids over active LLDP edges (canonical edges are undirected)."""
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


def resolve_upstream_chain(
    session: Session, access_node: NetworkDevice
) -> list[NetworkDevice]:
    """Shortest path of device hops from the access node to the nearest
    core-role node, via the LLDP graph. Returns [agg... core] (excludes the
    access node); empty if no core is reachable. Cycle-safe, hop-capped."""
    start = access_node.id
    visited = {start}
    parent: dict = {start: None}
    queue: deque = deque([(start, 0)])
    target = None
    while queue:
        nid, dist = queue.popleft()
        if nid != start:
            dev = session.get(NetworkDevice, nid)
            if dev is not None and dev.role == DeviceRole.core:
                target = nid
                break
        if dist >= _MAX_UPSTREAM_HOPS:
            continue
        for nb in _lldp_neighbor_ids(session, nid):
            if nb not in visited:
                visited.add(nb)
                parent[nb] = nid
                queue.append((nb, dist + 1))
    if target is None:
        return []
    chain_ids = []
    cur = target
    while cur is not None:
        chain_ids.append(cur)
        cur = parent[cur]
    chain_ids.reverse()  # start ... core
    return [  # drop the access node; keep [agg... core]
        dev
        for nid in chain_ids[1:]
        if (dev := session.get(NetworkDevice, nid)) is not None
    ]


def _active_ont_assignment(
    session: Session, subscription: Subscription
) -> OntAssignment | None:
    base = session.query(OntAssignment).filter(
        OntAssignment.subscriber_id == subscription.subscriber_id,
        OntAssignment.active.is_(True),
    )
    # NOTE: ont_assignments has a partial-unique index on ont_unit_id (active),
    # not on subscriber_id. This single-subscriber lookup is fine for a detail
    # page; a subscriber_id index is a follow-up if it becomes a hot path.
    if subscription.service_address_id is not None:
        by_addr = base.filter(
            OntAssignment.service_address_id == subscription.service_address_id
        ).first()
        if by_addr is not None:
            return by_addr
    return base.first()


def _node_for_device(
    session: Session, device_type: str, device_id
) -> NetworkDevice | None:
    return (
        session.query(NetworkDevice)
        .filter(
            NetworkDevice.matched_device_type == device_type,
            NetworkDevice.matched_device_id == device_id,
        )
        .first()
    )


def _finish(session: Session, path: CustomerPath, device_type: str) -> CustomerPath:
    """Walk device -> node -> basestation, recording the first gap."""
    assert path.access_device is not None  # callers set it before _finish
    node = _node_for_device(session, device_type, path.access_device.id)
    if node is None:
        path.gap = GAP_NO_NODE
        return path
    path.node = node
    path.upstream_chain = resolve_upstream_chain(session, node)
    if node.pop_site_id is None:
        path.gap = GAP_NO_BASESTATION
        return path
    path.basestation = session.get(PopSite, node.pop_site_id)
    if path.basestation is None:
        path.gap = GAP_NO_BASESTATION
    return path


def resolve_customer_path(session: Session, subscription: Subscription) -> CustomerPath:
    """Resolve ONT -> access device -> basestation for a subscription."""
    path = CustomerPath()

    # Fiber first: an active ONT assignment implies a fiber/OLT path.
    assignment = _active_ont_assignment(session, subscription)
    if assignment is not None:
        ont = session.get(OntUnit, assignment.ont_unit_id)
        path.ont = ont
        if ont is not None and ont.olt_device_id is not None:
            path.access_device = session.get(OLTDevice, ont.olt_device_id)
            path.access_device_kind = "olt"
        if path.access_device is None:
            path.gap = GAP_NO_NODE  # ONT exists but no OLT to anchor it
            return path
        return _finish(session, path, "olt")

    # Non-fiber: the subscription's provisioning NAS.
    if subscription.provisioning_nas_device_id is not None:
        path.access_device = session.get(
            NasDevice, subscription.provisioning_nas_device_id
        )
        path.access_device_kind = "nas"
        if path.access_device is None:
            path.gap = GAP_NO_ONT
            return path
        return _finish(session, path, "nas")

    # No resolvable access device at all.
    path.gap = GAP_NO_ONT
    return path
