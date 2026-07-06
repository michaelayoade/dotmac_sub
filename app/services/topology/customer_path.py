"""Resolve a subscription's end-to-end path: ONT -> access device -> basestation.

Pure read; never calls Zabbix. Walks the provisioning edges sub already owns to
a NetworkDevice node (linked by the reconcile's matched_device_*), then to the
node's pop_site (the basestation). Returns a partial result + a gap marker when
the chain breaks, so support sees *where* provisioning is incomplete rather than
a blank panel.
"""

from __future__ import annotations

import logging
from collections import deque
from dataclasses import dataclass, field
from typing import Any

from sqlalchemy import or_
from sqlalchemy.orm import Session

from app.models.catalog import NasDevice, Subscription
from app.models.network import (
    CPEDevice,
    DeviceStatus,
    FdhCabinet,
    OLTDevice,
    OntAssignment,
    OntUnit,
    PonPort,
    PonPortSplitterLink,
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
from app.models.radius_active_session import RadiusActiveSession
from app.services.topology.lldp_poller import SOURCE as LLDP_SOURCE

logger = logging.getLogger(__name__)

# Max hops to walk toward core (guards against pathological graphs).
_MAX_UPSTREAM_HOPS = 8

# Gap markers (None = complete path).
GAP_NO_ONT = "no_ont"  # no resolvable access device (provisioning incomplete)
GAP_NO_NODE = "no_node"  # device not matched to a topology node
GAP_NO_BASESTATION = "no_basestation"  # node not mapped to a basestation


@dataclass
class CustomerPath:
    ont: OntUnit | None = None
    ont_assignment: OntAssignment | None = None
    splitter_port: SplitterPort | None = None
    splitter: Splitter | None = None
    fdh: FdhCabinet | None = None
    pon_port: PonPort | None = None
    access_device: Any | None = None  # OLTDevice | NasDevice | NetworkDevice (AP)
    access_device_kind: str | None = None  # 'olt' | 'nas' | 'ap'
    # True when the nas arm resolved via a live RadiusActiveSession (where the
    # customer is connected right now) rather than the static provisioning NAS.
    # Lets the panel flag a live-session (roaming/failover) trace.
    live_session: bool = False
    # Wireless customer radio (UISP relationship layer); set on the 'ap' arm.
    radio: CPEDevice | None = None
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
    #
    # Order by id so a subscriber with >1 active ONT (and no service-address
    # match) resolves to a STABLE ONT — otherwise the customer-facing path and
    # the batched gap reader (which orders by id) could pick different ONTs,
    # hence different OLTs/basestations, run to run.
    base = base.order_by(OntAssignment.id)
    if subscription.service_address_id is not None:
        by_addr = base.filter(
            OntAssignment.service_address_id == subscription.service_address_id
        ).first()
        if by_addr is not None:
            return by_addr
    return base.first()


def _splitter_port_assignment_for_subscription(
    session: Session, subscription: Subscription
) -> SplitterPortAssignment | None:
    base = session.query(SplitterPortAssignment).filter(
        SplitterPortAssignment.active.is_(True)
    )
    if subscription.service_address_id is not None:
        by_addr = (
            base.filter(
                SplitterPortAssignment.service_address_id
                == subscription.service_address_id
            )
            .order_by(SplitterPortAssignment.id)
            .first()
        )
        if by_addr is not None:
            return by_addr
    return (
        base.filter(SplitterPortAssignment.subscriber_id == subscription.subscriber_id)
        .order_by(SplitterPortAssignment.id)
        .first()
    )


def _populate_fiber_plant(
    session: Session,
    path: CustomerPath,
    subscription: Subscription,
    assignment: OntAssignment,
) -> None:
    """Populate physical fiber plant fields from local database mappings only."""
    ont = path.ont
    path.ont_assignment = assignment

    if assignment.pon_port_id is not None:
        path.pon_port = session.get(PonPort, assignment.pon_port_id)
    if path.pon_port is None and ont is not None and ont.pon_port_id is not None:
        path.pon_port = session.get(PonPort, ont.pon_port_id)

    if ont is not None and ont.splitter_port_id is not None:
        path.splitter_port = session.get(SplitterPort, ont.splitter_port_id)
    if path.splitter_port is None:
        port_assignment = _splitter_port_assignment_for_subscription(
            session, subscription
        )
        if port_assignment is not None:
            path.splitter_port = session.get(
                SplitterPort, port_assignment.splitter_port_id
            )

    if path.splitter_port is not None:
        path.splitter = session.get(Splitter, path.splitter_port.splitter_id)
        if path.pon_port is None:
            pon_link = (
                session.query(PonPortSplitterLink)
                .filter(
                    PonPortSplitterLink.splitter_port_id == path.splitter_port.id,
                    PonPortSplitterLink.active.is_(True),
                )
                .first()
            )
            if pon_link is not None:
                path.pon_port = session.get(PonPort, pon_link.pon_port_id)

    if path.splitter is None and ont is not None and ont.splitter_id is not None:
        path.splitter = session.get(Splitter, ont.splitter_id)

    if path.splitter is not None and path.splitter.fdh_id is not None:
        path.fdh = session.get(FdhCabinet, path.splitter.fdh_id)

    if path.pon_port is not None:
        path.access_device = session.get(OLTDevice, path.pon_port.olt_id)
        path.access_device_kind = "olt" if path.access_device is not None else None


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
    return _finish_at_node(session, path, node)


def _finish_at_node(
    session: Session, path: CustomerPath, node: NetworkDevice
) -> CustomerPath:
    """Walk node -> basestation (+ upstream chain), recording the first gap."""
    path.node = node
    path.upstream_chain = resolve_upstream_chain(session, node)
    if node.pop_site_id is None:
        path.gap = GAP_NO_BASESTATION
        return path
    path.basestation = session.get(PopSite, node.pop_site_id)
    if path.basestation is None:
        path.gap = GAP_NO_BASESTATION
    return path


def _active_wireless_cpe(
    session: Session, subscription: Subscription
) -> CPEDevice | None:
    """The subscriber's active radio with a known parent AP, or None.

    "Active" = CPE row not retired/inactive AND UISP has not reported the
    device vanished ('active'/'disconnected'/NULL all count — a disconnected
    radio is still the customer's access path; NULL covers rows written
    before the status column existed). When several radios qualify, prefer
    the most recently UISP-synced one (then lowest id) so repeated calls
    resolve the same AP.
    """
    if subscription.subscriber_id is None:
        return None
    return (
        session.query(CPEDevice)
        .filter(
            CPEDevice.subscriber_id == subscription.subscriber_id,
            CPEDevice.parent_network_device_id.isnot(None),
            CPEDevice.status == DeviceStatus.active,
            or_(
                CPEDevice.last_uisp_status.is_(None),
                CPEDevice.last_uisp_status != "vanished",
            ),
        )
        .order_by(CPEDevice.uisp_synced_at.desc().nullslast(), CPEDevice.id)
        .first()
    )


def _live_session_nas_device_id(session: Session, subscription: Subscription):
    """The NAS the subscriber is CONNECTED to right now, per the live RADIUS
    sessions table, or None when there is no resolvable live session.

    Reads ``radius_active_sessions.nas_device_id`` (a UUID FK) — the reconciler
    already resolved each live session to a nas_device_id. No raw radacct or
    inet columns are read here, so there is no psycopg inet/ipaddress type to
    normalize (framed_ip_address/nas_ip_address are plain String and untouched).

    Roaming/failover mean this can differ from
    ``subscription.provisioning_nas_device_id`` (the static edge); when it does,
    the live value is where the customer actually terminates.

    A session explicitly bound to a *different* sibling subscription of the same
    subscriber is excluded (only this subscription's own session, or a session
    with no subscription binding, is eligible) — otherwise the known
    duplicate-login case (subscriber owns subs A and B, the session belongs to
    A) would report B as live on A's NAS. This mirrors affected.py, which joins
    strictly on ``subscription_id``. Among eligible sessions, prefers this
    subscription's own binding, then the freshest, so calls are stable.
    """
    if subscription.subscriber_id is None:
        return None
    row = (
        session.query(RadiusActiveSession.nas_device_id)
        .filter(
            RadiusActiveSession.subscriber_id == subscription.subscriber_id,
            RadiusActiveSession.nas_device_id.isnot(None),
            or_(
                RadiusActiveSession.subscription_id == subscription.id,
                RadiusActiveSession.subscription_id.is_(None),
            ),
        )
        .order_by(
            # Prefer a session for this exact subscription, then the freshest.
            (RadiusActiveSession.subscription_id == subscription.id).desc(),
            RadiusActiveSession.last_update.desc().nullslast(),
            RadiusActiveSession.session_start.desc(),
            RadiusActiveSession.id,
        )
        .first()
    )
    return row[0] if row is not None else None


def _resolve_nas_arm(
    session: Session,
    subscription: Subscription,
    nas_device_id,
    *,
    live: bool,
) -> CustomerPath:
    """Resolve the NAS arm for one NasDevice id into a fresh CustomerPath.

    Reuses the existing NasDevice -> node -> basestation machinery (``_finish``).
    ``path.gap is None`` iff it resolved to a complete E2E path. ``live`` stamps
    the ``live_session`` marker so a caller can tell which NAS won.
    """
    path = CustomerPath()
    path.access_device = session.get(NasDevice, nas_device_id)
    path.access_device_kind = "nas"
    path.live_session = live
    if path.access_device is None:
        path.gap = GAP_NO_ONT
        return path
    return _finish(session, path, "nas")


def resolve_customer_path(session: Session, subscription: Subscription) -> CustomerPath:
    """Resolve ONT -> access device -> basestation for a subscription."""
    path = CustomerPath()

    # Fiber first: an active ONT assignment implies a fiber/OLT path.
    assignment = _active_ont_assignment(session, subscription)
    if assignment is not None:
        ont = session.get(OntUnit, assignment.ont_unit_id)
        path.ont = ont
        _populate_fiber_plant(session, path, subscription, assignment)
        if ont is not None and ont.olt_device_id is not None:
            path.access_device = session.get(OLTDevice, ont.olt_device_id)
            path.access_device_kind = "olt"
        if path.access_device is None:
            path.gap = GAP_NO_NODE  # ONT exists but no OLT to anchor it
            return path
        return _finish(session, path, "olt")

    # Wireless: an active radio with a known parent AP (the UISP relationship
    # layer's CPE -> AP edge). Tried before the NAS arm because it is finer:
    # a wireless subscriber's PPPoE often terminates on a NAS at the BTS, and
    # the NAS arm would resolve them coarsely (NAS -> basestation) while the
    # radio arm pins them to their actual AP.
    cpe = _active_wireless_cpe(session, subscription)
    if cpe is not None:
        node = session.get(NetworkDevice, cpe.parent_network_device_id)
        if node is not None:
            path.radio = cpe
            path.access_device = node
            path.access_device_kind = "ap"
            return _finish_at_node(session, path, node)
        # Parent edge points nowhere (row deleted mid-sync): fall through to
        # the NAS arm rather than dead-ending a resolvable subscriber.

    # Non-fiber: the NAS the customer terminates on. Prefer the LIVE session's
    # NAS (where they are connected right now) over the static provisioning NAS
    # — roaming/failover mean the two can differ, and the live one is the truth
    # for a currently-online customer.
    #
    # But the live NAS only wins when it resolves to a COMPLETE path: a
    # roaming/failover customer can land on a NAS not yet Zabbix-matched (no
    # topology node), which today the static provisioning NAS still resolves.
    # So if the live NAS fails to resolve (device row or topology node missing),
    # fall back to the static provisioning NAS before recording any gap — a live
    # session must never *regress* a subscription that resolves statically. A
    # gap is only recorded when BOTH the live and the static NAS fail.
    live_nas_id = _live_session_nas_device_id(session, subscription)
    static_nas_id = subscription.provisioning_nas_device_id

    live_path: CustomerPath | None = None
    if live_nas_id is not None:
        live_path = _resolve_nas_arm(session, subscription, live_nas_id, live=True)
        if live_path.gap is None:
            # Adoption signal: how often live beats the static provisioning NAS.
            logger.debug(
                "customer_path: subscription %s resolved via live-session NAS "
                "%s (provisioning NAS %s)",
                subscription.id,
                live_nas_id,
                static_nas_id,
            )
            return live_path
        logger.debug(
            "customer_path: subscription %s live-session NAS %s did not resolve "
            "(gap=%s); falling back to provisioning NAS %s",
            subscription.id,
            live_nas_id,
            live_path.gap,
            static_nas_id,
        )

    if static_nas_id is not None:
        # Static fallback: its outcome is exactly the pre-live behavior, so a
        # live session can only ever improve, never worsen, the verdict.
        return _resolve_nas_arm(session, subscription, static_nas_id, live=False)

    if live_path is not None:
        # The live NAS was the ONLY access device (no provisioning NAS on the
        # sub): keep its partial result rather than claiming no device at all.
        return live_path

    # No resolvable access device at all.
    path.gap = GAP_NO_ONT
    return path
