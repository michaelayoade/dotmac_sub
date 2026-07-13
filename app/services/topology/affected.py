"""Reverse traversal: infrastructure -> affected customers (Phase 4a).

The mirror of resolve_customer_path. Given a failing node or basestation,
enumerate the active subscriptions downstream of it — the engine for outage
impact assessment. Read-only; manual use (no auto-detection). An upstream
node expands to its downstream access nodes via the LLDP graph (moving away
from core), so an aggregation/core failure captures everything below it; with
no usable graph it degrades safely to the node itself.
"""

from __future__ import annotations

import logging
from collections import deque

from sqlalchemy import or_
from sqlalchemy.orm import Session

from app.models.catalog import Subscription, SubscriptionStatus
from app.models.network import (
    CPEDevice,
    DeviceStatus,
    FdhCabinet,
    OLTDevice,
    OntAssignment,
    OntUnit,
    PonPort,
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
from app.models.subscriber import Address
from app.services.network.signal_thresholds import (
    classify_signal,
    normalize_optical_signal_dbm,
)
from app.services.topology.lldp_poller import SOURCE as LLDP_SOURCE

logger = logging.getLogger(__name__)


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


def list_network_nodes(session: Session) -> list[NetworkDevice]:
    """Active network devices for outage-impact pickers."""
    return (
        session.query(NetworkDevice)
        .filter(NetworkDevice.is_active.is_(True))
        .order_by(NetworkDevice.name)
        .all()
    )


def lldp_adjacency(session: Session) -> dict:
    """Whole-graph undirected adjacency over active LLDP edges, from ONE query.

    The graph walks below take this as an optional precomputed parameter so a
    fleet-wide sweep (e.g. the outage auto-detect scan, which walks the graph
    for several candidates in one run) issues one links query total instead of
    one per visited node per walk.
    """
    adjacency: dict = {}
    for source_id, target_id in (
        session.query(
            NetworkTopologyLink.source_device_id, NetworkTopologyLink.target_device_id
        )
        .filter(
            NetworkTopologyLink.source == LLDP_SOURCE,
            NetworkTopologyLink.is_active.is_(True),
        )
        .all()
    ):
        adjacency.setdefault(source_id, set()).add(target_id)
        adjacency.setdefault(target_id, set()).add(source_id)
    return adjacency


def _dist_to_core(session: Session, *, adjacency: dict | None = None) -> dict:
    """BFS hop-distance to the nearest core node over the LLDP graph.

    One links query (via ``lldp_adjacency``) + one cores query; pass a
    precomputed ``adjacency`` to skip the links query too.
    """
    cores = (
        session.query(NetworkDevice)
        .filter(
            NetworkDevice.role == DeviceRole.core, NetworkDevice.is_active.is_(True)
        )
        .all()
    )
    if adjacency is None:
        adjacency = lldp_adjacency(session)
    dist: dict = {c.id: 0 for c in cores}
    queue: deque = deque((c.id, 0) for c in cores)
    while queue:
        nid, d = queue.popleft()
        for nb in adjacency.get(nid, ()):
            if nb not in dist:
                dist[nb] = d + 1
                queue.append((nb, d + 1))
    return dist


def downstream_nodes(
    session: Session,
    root: NetworkDevice,
    *,
    dist: dict | None = None,
    adjacency: dict | None = None,
) -> set:
    """Node ids at/below ``root`` — root plus nodes reachable moving strictly
    away from core (increasing distance-to-core). Degrades to {root} when the
    graph/core is unknown, so we never over-scope an outage.

    ``dist`` is the (root-independent) distance-to-core map and ``adjacency``
    the LLDP adjacency; callers that invoke this repeatedly in one request or
    scan can compute them once (``_dist_to_core`` / ``lldp_adjacency``) and
    pass them in to avoid a full-graph BFS + neighbor queries per call.
    """
    if adjacency is None:
        adjacency = lldp_adjacency(session)
    if dist is None:
        dist = _dist_to_core(session, adjacency=adjacency)
    result = {root.id}
    queue: deque = deque([root.id])
    while queue:
        nid = queue.popleft()
        cur_d = dist.get(nid)
        for nb in adjacency.get(nid, ()):
            if nb in result:
                continue
            nb_d = dist.get(nb)
            if cur_d is None or nb_d is None:
                continue  # unorderable -> don't expand (avoid over-scoping)
            if nb_d > cur_d:
                result.add(nb)
                queue.append(nb)
    return result


def subscriptions_for_nodes(
    session: Session, node_ids
) -> dict[object, list[Subscription]]:
    """Active subscriptions per node, as {node_id: [subscriptions]} (deduped).

    Batched mirror of ``subscriptions_for_node``: one query per arm across
    ALL the given nodes (instead of up to five per node), so basestation
    sweeps and impact previews stay O(arms), not O(nodes).

    Four additive arms, deduped by subscription id per node — an AP node may
    *also* be reconcile-matched as a NAS:
      - nas: subscriptions provisioned on the node's matched NAS
        (Subscription.provisioning_nas_device_id — the STATIC edge);
      - live: subscriptions with a live ``RadiusActiveSession`` on the node's
        matched NAS (where the customer is ACTUALLY connected right now).
        Additive to the nas arm and deduped by subscription id — roaming and
        failover mean a customer can be online on a NAS other than the one
        provisioned, so "NAS down -> affected" reflects who is currently
        connected there, not only who is provisioned there;
      - olt: subscriptions with an active ONT assignment on the matched OLT;
      - wireless: subscriptions whose active radio is parented to the node
        via the UISP CPE -> AP edge. "Active" mirrors
        customer_path._active_wireless_cpe: CPE row status is active and UISP
        has not reported the radio vanished (disconnected still counts — that
        radio is still the customer's access path).
    """
    node_ids = list(node_ids)
    if not node_ids:
        return {}
    result: dict[object, dict] = {nid: {} for nid in node_ids}

    nodes = session.query(NetworkDevice).filter(NetworkDevice.id.in_(node_ids)).all()

    # subscriber_id -> node_ids the subscriber-keyed arms (olt/wireless)
    # resolve them under; one Subscription fetch covers both arms.
    subscriber_nodes: dict[object, set] = {}

    def _credit(subscriber_id, nid) -> None:
        subscriber_nodes.setdefault(subscriber_id, set()).add(nid)

    # --- NAS arm (keyed by provisioning_nas_device_id, not subscriber) ---
    nas_node_ids: dict[object, list] = {}
    for n in nodes:
        if n.matched_device_type == "nas" and n.matched_device_id is not None:
            nas_node_ids.setdefault(n.matched_device_id, []).append(n.id)
    nas_subs: list[Subscription] = []
    # Live-session arm: (Subscription, live nas_device_id) pairs — the NAS the
    # customer is connected to per radius_active_sessions, keyed the same way as
    # provisioning (NasDevice.id -> matched nas node). Reading nas_device_id (a
    # UUID FK) only — no raw radacct/inet columns are touched here.
    live_rows: list[tuple] = []
    if nas_node_ids:
        nas_subs = (
            session.query(Subscription)
            .filter(
                Subscription.provisioning_nas_device_id.in_(nas_node_ids),
                Subscription.status == SubscriptionStatus.active,
            )
            .all()
        )
        live_rows = (
            session.query(Subscription, RadiusActiveSession.nas_device_id)
            .join(
                RadiusActiveSession,
                RadiusActiveSession.subscription_id == Subscription.id,
            )
            .filter(
                RadiusActiveSession.nas_device_id.in_(nas_node_ids),
                Subscription.status == SubscriptionStatus.active,
            )
            .tuples()
            .all()
        )

    # --- OLT arm ---
    olt_node_ids: dict[object, list] = {}
    for n in nodes:
        if n.matched_device_type == "olt" and n.matched_device_id is not None:
            olt_node_ids.setdefault(n.matched_device_id, []).append(n.id)
    if olt_node_ids:
        ont_to_olt = {
            r[0]: r[1]
            for r in session.query(OntUnit.id, OntUnit.olt_device_id)
            .filter(OntUnit.olt_device_id.in_(olt_node_ids))
            .all()
        }
        if ont_to_olt:
            for subscriber_id, ont_unit_id in (
                session.query(OntAssignment.subscriber_id, OntAssignment.ont_unit_id)
                .filter(
                    OntAssignment.ont_unit_id.in_(ont_to_olt),
                    OntAssignment.active.is_(True),
                    OntAssignment.subscriber_id.isnot(None),
                )
                .all()
            ):
                for nid in olt_node_ids[ont_to_olt[ont_unit_id]]:
                    _credit(subscriber_id, nid)

    # --- Wireless arm (works whether or not the node is reconcile-matched —
    #     the UISP CPE -> AP edge alone makes it an AP) ---
    for subscriber_id, parent_id in (
        session.query(CPEDevice.subscriber_id, CPEDevice.parent_network_device_id)
        .filter(
            CPEDevice.parent_network_device_id.in_(node_ids),
            CPEDevice.subscriber_id.isnot(None),
            CPEDevice.status == DeviceStatus.active,
            or_(
                CPEDevice.last_uisp_status.is_(None),
                CPEDevice.last_uisp_status != "vanished",
            ),
        )
        .distinct()
        .all()
    ):
        _credit(subscriber_id, parent_id)

    if subscriber_nodes:
        for sub in (
            session.query(Subscription)
            .filter(
                Subscription.subscriber_id.in_(subscriber_nodes),
                Subscription.status == SubscriptionStatus.active,
            )
            .all()
        ):
            for nid in subscriber_nodes[sub.subscriber_id]:
                result[nid][sub.id] = sub
    for sub in nas_subs:
        for nid in nas_node_ids[sub.provisioning_nas_device_id]:
            result[nid][sub.id] = sub
    live_added = 0
    for sub, nas_device_id in live_rows:
        for nid in nas_node_ids[nas_device_id]:
            if sub.id not in result[nid]:
                live_added += 1
            result[nid][sub.id] = sub
    if live_added:
        # Adoption signal: subscriptions the live-session arm contributed on
        # top of the static provisioning arm (roaming/failover/online-now).
        logger.debug(
            "affected: live-session arm added %d subscription(s) not covered "
            "by provisioning_nas across %d node(s)",
            live_added,
            len(node_ids),
        )

    return {nid: list(by_id.values()) for nid, by_id in result.items()}


def subscriptions_for_node(session: Session, node: NetworkDevice) -> list[Subscription]:
    """Active subscriptions whose access path terminates at this node.

    Thin single-node wrapper over ``subscriptions_for_nodes`` (the batched
    resolver); the arms and their union/dedupe semantics live there.
    """
    return subscriptions_for_nodes(session, [node.id]).get(node.id, [])


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


def _format_address(address: Address | None) -> str | None:
    if address is None:
        return None
    parts = [
        address.address_line1,
        address.address_line2,
        address.city,
        address.region,
        address.postal_code,
        address.country_code,
    ]
    return ", ".join(str(part).strip() for part in parts if str(part or "").strip())


def _subscriber_address(subscription: Subscription) -> str | None:
    subscriber = getattr(subscription, "subscriber", None)
    if subscriber is None:
        return None
    parts = [
        subscriber.address_line1,
        subscriber.address_line2,
        subscriber.city,
        subscriber.region,
        subscriber.postal_code,
        subscriber.country_code,
    ]
    return ", ".join(str(part).strip() for part in parts if str(part or "").strip())


def _enum_value(value) -> str | None:
    if value is None:
        return None
    return getattr(value, "value", str(value))


def _signal_summary(ont: OntUnit | None) -> dict[str, object | None]:
    if ont is None:
        return {
            "status": None,
            "quality": "unknown",
            "onu_rx_dbm": None,
            "olt_rx_dbm": None,
            "last_seen_at": None,
        }
    onu_rx = normalize_optical_signal_dbm(getattr(ont, "onu_rx_signal_dbm", None))
    olt_rx = normalize_optical_signal_dbm(getattr(ont, "olt_rx_signal_dbm", None))
    from app.services.network.ont_status import resolve_effective_ont_status

    effective = resolve_effective_ont_status(ont)
    return {
        "status": effective.status.value,
        "raw_olt_status": _enum_value(getattr(ont, "olt_status", None)),
        "status_retry_pending": effective.retry_pending,
        "quality": classify_signal(onu_rx),
        "onu_rx_dbm": onu_rx,
        "olt_rx_dbm": olt_rx,
        "last_seen_at": getattr(ont, "last_seen_at", None)
        or getattr(ont, "olt_status_seen_at", None)
        or getattr(ont, "signal_updated_at", None),
    }


def fdh_impact_rows(session: Session, fdh: FdhCabinet) -> list[dict]:
    """Detailed, read-only FDH impact rows for operator/API surfaces.

    Reuses the same FDH subscription resolver as ``affected_customers`` so the
    summary count and detailed rows stay aligned. Missing topology is rendered
    as ``None`` instead of filtering the customer out.
    """
    subscriptions = subscriptions_for_fdh(session, fdh)
    if not subscriptions:
        return []

    splitter_ids = [
        row[0]
        for row in session.query(Splitter.id)
        .filter(Splitter.fdh_id == fdh.id, Splitter.is_active.is_(True))
        .all()
    ]
    splitter_port_ids = (
        [
            row[0]
            for row in session.query(SplitterPort.id)
            .filter(
                SplitterPort.splitter_id.in_(splitter_ids),
                SplitterPort.is_active.is_(True),
            )
            .all()
        ]
        if splitter_ids
        else []
    )

    subscriber_ids = {sub.subscriber_id for sub in subscriptions if sub.subscriber_id}
    service_address_ids = {
        sub.service_address_id for sub in subscriptions if sub.service_address_id
    }

    splitter_port_assignments = []
    if splitter_port_ids:
        splitter_port_assignments = (
            session.query(SplitterPortAssignment)
            .filter(
                SplitterPortAssignment.splitter_port_id.in_(splitter_port_ids),
                SplitterPortAssignment.active.is_(True),
            )
            .all()
        )
        service_address_ids.update(
            assignment.service_address_id
            for assignment in splitter_port_assignments
            if assignment.service_address_id is not None
        )

    assignment_filters = []
    if subscriber_ids:
        assignment_filters.append(OntAssignment.subscriber_id.in_(subscriber_ids))
    if service_address_ids:
        assignment_filters.append(
            OntAssignment.service_address_id.in_(service_address_ids)
        )
    if splitter_port_ids:
        assignment_filters.append(OntUnit.splitter_port_id.in_(splitter_port_ids))
    if splitter_ids:
        assignment_filters.append(OntUnit.splitter_id.in_(splitter_ids))

    ont_assignments = []
    if assignment_filters:
        ont_assignments = (
            session.query(OntAssignment)
            .join(OntUnit, OntUnit.id == OntAssignment.ont_unit_id)
            .filter(
                OntAssignment.active.is_(True),
                or_(*assignment_filters),
            )
            .all()
        )

    ont_by_id = {
        assignment.ont_unit_id: session.get(OntUnit, assignment.ont_unit_id)
        for assignment in ont_assignments
    }
    ont_assignment_by_subscriber = {
        assignment.subscriber_id: assignment
        for assignment in ont_assignments
        if assignment.subscriber_id is not None
    }
    ont_assignment_by_address = {
        assignment.service_address_id: assignment
        for assignment in ont_assignments
        if assignment.service_address_id is not None
    }
    port_assignment_by_subscriber = {
        assignment.subscriber_id: assignment
        for assignment in splitter_port_assignments
        if assignment.subscriber_id is not None
    }
    port_assignment_by_address = {
        assignment.service_address_id: assignment
        for assignment in splitter_port_assignments
        if assignment.service_address_id is not None
    }

    rows: list[dict] = []
    for subscription in subscriptions:
        subscriber = getattr(subscription, "subscriber", None)
        ont_assignment = (
            ont_assignment_by_subscriber.get(subscription.subscriber_id)
            if subscription.subscriber_id is not None
            else None
        ) or (
            ont_assignment_by_address.get(subscription.service_address_id)
            if subscription.service_address_id is not None
            else None
        )
        port_assignment = (
            port_assignment_by_subscriber.get(subscription.subscriber_id)
            if subscription.subscriber_id is not None
            else None
        ) or (
            port_assignment_by_address.get(subscription.service_address_id)
            if subscription.service_address_id is not None
            else None
        )

        ont = ont_by_id.get(ont_assignment.ont_unit_id) if ont_assignment else None
        splitter_port = None
        splitter = None
        if ont is not None and ont.splitter_port_id is not None:
            splitter_port = session.get(SplitterPort, ont.splitter_port_id)
        if splitter_port is None and port_assignment is not None:
            splitter_port = session.get(SplitterPort, port_assignment.splitter_port_id)
        if splitter_port is not None:
            splitter = session.get(Splitter, splitter_port.splitter_id)
        if splitter is None and ont is not None and ont.splitter_id is not None:
            splitter = session.get(Splitter, ont.splitter_id)

        pon_port = None
        if ont_assignment is not None and ont_assignment.pon_port_id is not None:
            pon_port = session.get(PonPort, ont_assignment.pon_port_id)
        if pon_port is None and ont is not None and ont.pon_port_id is not None:
            pon_port = session.get(PonPort, ont.pon_port_id)

        olt = None
        if pon_port is not None:
            olt = session.get(OLTDevice, pon_port.olt_id)
        if olt is None and ont is not None and ont.olt_device_id is not None:
            olt = session.get(OLTDevice, ont.olt_device_id)

        service_address = getattr(subscription, "service_address", None)
        if service_address is None and ont_assignment is not None:
            service_address = getattr(ont_assignment, "service_address", None)
        if service_address is None and port_assignment is not None:
            service_address = getattr(port_assignment, "service_address", None)

        signal = _signal_summary(ont)
        rows.append(
            {
                "subscription_id": subscription.id,
                "subscription_status": _enum_value(subscription.status),
                "subscriber_id": subscription.subscriber_id,
                "subscriber_name": (
                    " ".join(
                        part
                        for part in [
                            getattr(subscriber, "first_name", None),
                            getattr(subscriber, "last_name", None),
                        ]
                        if part
                    )
                    or getattr(subscriber, "display_name", None)
                    or getattr(subscriber, "company_name", None)
                    or None
                ),
                "subscriber_number": getattr(subscriber, "subscriber_number", None)
                or getattr(subscriber, "account_number", None),
                "email": getattr(subscriber, "email", None),
                "phone": getattr(subscriber, "phone", None),
                "service_address": _format_address(service_address)
                or _subscriber_address(subscription),
                "ont_id": getattr(ont, "id", None),
                "ont_serial": getattr(ont, "serial_number", None),
                "olt_id": getattr(olt, "id", None),
                "olt_name": getattr(olt, "name", None),
                "pon_port_id": getattr(pon_port, "id", None),
                "pon_port_name": getattr(pon_port, "name", None),
                "splitter_id": getattr(splitter, "id", None),
                "splitter_name": getattr(splitter, "name", None),
                "splitter_port_id": getattr(splitter_port, "id", None),
                "splitter_port_number": getattr(splitter_port, "port_number", None),
                "signal_status": signal["status"],
                "signal_quality": signal["quality"],
                "onu_rx_dbm": signal["onu_rx_dbm"],
                "olt_rx_dbm": signal["olt_rx_dbm"],
                "last_seen_at": signal["last_seen_at"],
            }
        )
    return sorted(
        rows,
        key=lambda row: (
            str(row.get("splitter_name") or ""),
            row.get("splitter_port_number") or 0,
            str(row.get("subscriber_name") or ""),
        ),
    )


def affected_customers(
    session: Session,
    node: NetworkDevice | None = None,
    basestation: PopSite | None = None,
    fdh: FdhCabinet | None = None,
    *,
    dist: dict | None = None,
    adjacency: dict | None = None,
) -> dict:
    """Subscriptions affected by a failing node and/or basestation.

    Returns {subscriptions, node_ids, subscriptions_by_node, count,
    online_by_node, online_count} (deduped; subscriptions_by_node lets callers
    do per-node coverage checks without re-resolving). For a basestation, all
    its active nodes; for a node, it + its downstream access nodes.

    ``dist``/``adjacency`` are optional precomputed graph maps (see
    ``downstream_nodes``) for callers resolving many scopes in one run.

    ``online_by_node`` / ``online_count`` are the proof-of-life overlay
    (outage classifier P1, design §2): how many affected subscriptions have a
    FRESH live RADIUS session per node, and in total. ``online >= 1`` behind a
    node vetoes "down" for that node and everything upstream (design §0).
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
        node_ids |= downstream_nodes(session, node, dist=dist, adjacency=adjacency)

    subs: dict = {}
    if fdh is not None:
        for s in subscriptions_for_fdh(session, fdh):
            subs[s.id] = s
    subscriptions_by_node = subscriptions_for_nodes(session, node_ids)
    for node_subs in subscriptions_by_node.values():
        for s in node_subs:
            subs[s.id] = s

    # Proof-of-life overlay (outage classifier P1, design §2). Local import
    # keeps module load acyclic: health_classifier imports helpers from here.
    from app.services.topology.health_classifier import online_subscription_ids

    online_ids = online_subscription_ids(session, subs.keys())
    online_by_node = {
        nid: sum(1 for s in node_subs if s.id in online_ids)
        for nid, node_subs in subscriptions_by_node.items()
    }
    return {
        "subscriptions": list(subs.values()),
        "node_ids": node_ids,
        "subscriptions_by_node": subscriptions_by_node,
        "count": len(subs),
        "online_by_node": online_by_node,
        "online_count": len(online_ids),
    }


def _with_bar_pct(rows: list[dict]) -> list[dict]:
    """Stamp a ``pct`` (0-100) on each row relative to the busiest branch, for
    the impact tree's proportional bars. Mutates and returns ``rows``."""
    max_count = max((r["count"] for r in rows), default=0)
    for row in rows:
        row["pct"] = round(100 * row["count"] / max_count) if max_count else 0
    return rows


def impact_breakdown(session: Session, result: dict) -> list[dict]:
    """Per-node blast-radius rows for the outage-impact tree.

    Each node in the failure domain with its affected active-subscription
    count, live status (for the dot) and a bar percentage. Derived from an
    ``affected_customers`` result — no re-resolution. Nodes with zero affected
    subscriptions are dropped (the header already carries the total; the tree
    shows *where* the impact lands). Sorted by count desc, then name.
    """
    by_node: dict = result.get("subscriptions_by_node", {})
    rows: list[dict] = []
    for nid in result.get("node_ids", set()):
        count = len(by_node.get(nid, []))
        if count == 0:
            continue
        node = session.get(NetworkDevice, nid)
        if node is None:
            continue
        rows.append(
            {
                "id": nid,
                "name": node.name,
                "role": _enum_value(node.role),
                "live_status": node.live_status or "unknown",
                "live_status_at": node.live_status_at,
                "count": count,
            }
        )
    rows.sort(key=lambda r: (-r["count"], r["name"].lower()))
    return _with_bar_pct(rows)


def fdh_impact_branches(rows: list[dict]) -> list[dict]:
    """Aggregate ``fdh_impact_rows`` (one row per customer) into per-splitter
    blast-radius branches: cabinet -> splitter -> affected count. Passive
    plant, so ``live_status`` is neutral. Sorted by count desc, then name.
    """
    counts: dict[str, int] = {}
    for row in rows:
        name = row.get("splitter_name") or "—"
        counts[name] = counts.get(name, 0) + 1
    branches: list[dict] = [
        {"name": name, "count": count, "live_status": "plant"}
        for name, count in counts.items()
    ]
    branches.sort(key=lambda b: (-b["count"], b["name"].lower()))
    return _with_bar_pct(branches)
