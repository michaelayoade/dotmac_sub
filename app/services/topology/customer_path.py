"""Resolve a subscription's end-to-end path: ONT -> access device -> basestation.

Pure read; never calls Zabbix. Walks the provisioning edges sub already owns to
a NetworkDevice node (linked by the reconcile's matched_device_*), then to the
node's pop_site (the basestation). Returns a partial result + a gap marker when
the chain breaks, so support sees *where* provisioning is incomplete rather than
a blank panel.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from sqlalchemy.orm import Session

from app.models.catalog import NasDevice, Subscription
from app.models.network import OLTDevice, OntAssignment, OntUnit
from app.models.network_monitoring import NetworkDevice, PopSite

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
    gap: str | None = None


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
    node = _node_for_device(session, device_type, path.access_device.id)
    if node is None:
        path.gap = GAP_NO_NODE
        return path
    path.node = node
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
