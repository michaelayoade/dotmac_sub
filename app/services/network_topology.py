"""Network topology service — link CRUD, graph projection, utilization."""

from __future__ import annotations

import logging
from uuid import UUID

from fastapi import HTTPException
from sqlalchemy import func, select
from sqlalchemy.orm import Session, joinedload

from app.models.network_monitoring import (
    DeviceInterface,
    DeviceMetric,
    DeviceRole,
    DeviceType,
    MetricType,
    NetworkDevice,
    NetworkTopologyLink,
    PopSite,
    TopologyLinkAdminStatus,
    TopologyLinkMedium,
    TopologyLinkRole,
)
from app.services.common import coerce_uuid

logger = logging.getLogger(__name__)


TOPOLOGY_DEFAULT_ROLES = {
    DeviceRole.core,
    DeviceRole.distribution,
    DeviceRole.aggregation,
    DeviceRole.access,
    DeviceRole.edge,
}

TOPOLOGY_DEFAULT_TYPES = {
    DeviceType.router,
    DeviceType.switch,
    DeviceType.firewall,
    DeviceType.bridge,
    DeviceType.hub,
}


# ── CRUD ─────────────────────────────────────────────────────────────


class TopologyLinks:
    """Manager for network topology link records."""

    @staticmethod
    def list(
        db: Session,
        *,
        source_device_id: str | None = None,
        target_device_id: str | None = None,
        link_role: str | None = None,
        topology_group: str | None = None,
        bundle_key: str | None = None,
        is_active: bool | None = True,
        limit: int = 500,
        offset: int = 0,
    ) -> list[NetworkTopologyLink]:
        stmt = (
            select(NetworkTopologyLink)
            .options(
                joinedload(NetworkTopologyLink.source_device),
                joinedload(NetworkTopologyLink.target_device),
                joinedload(NetworkTopologyLink.source_interface),
                joinedload(NetworkTopologyLink.target_interface),
            )
        )
        if source_device_id:
            stmt = stmt.where(NetworkTopologyLink.source_device_id == coerce_uuid(source_device_id))
        if target_device_id:
            stmt = stmt.where(NetworkTopologyLink.target_device_id == coerce_uuid(target_device_id))
        if link_role:
            stmt = stmt.where(NetworkTopologyLink.link_role == TopologyLinkRole(link_role))
        if topology_group:
            stmt = stmt.where(NetworkTopologyLink.topology_group == topology_group)
        if bundle_key:
            stmt = stmt.where(NetworkTopologyLink.bundle_key == bundle_key)
        if is_active is not None:
            stmt = stmt.where(NetworkTopologyLink.is_active.is_(is_active))
        stmt = stmt.order_by(NetworkTopologyLink.created_at.desc()).offset(offset).limit(limit)
        return list(db.scalars(stmt).unique().all())

    @staticmethod
    def get(db: Session, link_id: str) -> NetworkTopologyLink:
        link = db.get(NetworkTopologyLink, coerce_uuid(link_id))
        if not link:
            raise HTTPException(status_code=404, detail="Topology link not found")
        return link

    @staticmethod
    def create(db: Session, *, data: dict) -> NetworkTopologyLink:
        """Create a new topology link with validation."""
        normalized = _normalize_link_data(data)
        _validate_link_data(db, normalized)
        link = NetworkTopologyLink(
            **normalized,
        )
        db.add(link)
        db.commit()
        db.refresh(link)
        return link

    @staticmethod
    def update(db: Session, link_id: str, *, data: dict) -> NetworkTopologyLink:
        link = TopologyLinks.get(db, link_id)
        normalized = _normalize_link_data(data)
        _validate_link_data(db, normalized, current_link_id=link_id)
        for field, value in normalized.items():
            setattr(link, field, value)
        db.commit()
        db.refresh(link)
        return link

    @staticmethod
    def delete(db: Session, link_id: str) -> None:
        link = TopologyLinks.get(db, link_id)
        link.is_active = False
        db.commit()


def _normalize_link_data(data: dict) -> dict:
    return {
        "source_device_id": coerce_uuid(data["source_device_id"]),
        "source_interface_id": coerce_uuid(data.get("source_interface_id")) if data.get("source_interface_id") else None,
        "target_device_id": coerce_uuid(data["target_device_id"]),
        "target_interface_id": coerce_uuid(data.get("target_interface_id")) if data.get("target_interface_id") else None,
        "link_role": TopologyLinkRole(data.get("link_role", "unknown")),
        "medium": TopologyLinkMedium(data.get("medium", "unknown")),
        "capacity_bps": int(data["capacity_bps"]) if data.get("capacity_bps") else None,
        "bundle_key": data.get("bundle_key") or None,
        "topology_group": data.get("topology_group") or None,
        "admin_status": TopologyLinkAdminStatus(data.get("admin_status", "enabled")),
        "notes": data.get("notes") or None,
    }


def _validate_link_data(db: Session, data: dict, *, current_link_id: str | None = None) -> None:
    """Validate that endpoints exist and interfaces aren't reused."""
    src_dev = db.get(NetworkDevice, data["source_device_id"])
    if not src_dev:
        raise ValueError("Source device not found")
    tgt_dev = db.get(NetworkDevice, data["target_device_id"])
    if not tgt_dev:
        raise ValueError("Target device not found")
    if data["source_device_id"] == data["target_device_id"] and (
        not data.get("source_interface_id") or data.get("source_interface_id") == data.get("target_interface_id")
    ):
        raise ValueError("Source and target endpoints must be distinct")

    # Check interface reuse — an interface can only be in ONE active link
    for iface_key in ("source_interface_id", "target_interface_id"):
        iface_id = data.get(iface_key)
        if not iface_id:
            continue
        stmt = select(NetworkTopologyLink).where(
            NetworkTopologyLink.is_active.is_(True),
            (
                (NetworkTopologyLink.source_interface_id == iface_id)
                | (NetworkTopologyLink.target_interface_id == iface_id)
            ),
        )
        if current_link_id:
            stmt = stmt.where(NetworkTopologyLink.id != coerce_uuid(current_link_id))
        existing = db.scalars(stmt).first()
        if existing:
            iface = db.get(DeviceInterface, iface_id)
            iface_name = iface.name if iface else str(iface_id)[:8]
            raise ValueError(f"Interface {iface_name} is already used in another active link")


# ── Graph Projection ─────────────────────────────────────────────────


def list_nodes_and_edges(
    db: Session,
    *,
    topology_group: str | None = None,
    pop_site_id: str | None = None,
    include_utilization: bool = True,
) -> dict:
    """Build the full topology graph as nodes + edges for D3.js.

    Returns:
        {
            "nodes": [{"id", "name", "status", "device_type", "ip", ...}],
            "edges": [{"id", "source", "target", "role", "medium", "capacity_bps",
                        "bundle_key", "utilization", "rx_bps", "tx_bps", ...}],
            "bundles": {"key": [edge_ids]},
        }
    """
    links = TopologyLinks.list(db, topology_group=topology_group, is_active=True, limit=2000)

    # Collect unique device IDs
    device_ids: set[UUID] = set()
    for link in links:
        device_ids.add(link.source_device_id)
        device_ids.add(link.target_device_id)

    # Build node list. When no links exist yet, fall back to active inventory so the
    # topology page remains useful as a starting canvas instead of rendering empty.
    if device_ids:
        stmt = (
            select(NetworkDevice)
            .options(joinedload(NetworkDevice.pop_site))
            .where(NetworkDevice.id.in_(device_ids))
        )
        if pop_site_id:
            stmt = stmt.where(NetworkDevice.pop_site_id == coerce_uuid(pop_site_id))
        devices = list(db.scalars(stmt).all())
    else:
        stmt = (
            select(NetworkDevice)
            .options(joinedload(NetworkDevice.pop_site))
            .where(NetworkDevice.is_active.is_(True))
            .where(
                (
                    NetworkDevice.role.in_(TOPOLOGY_DEFAULT_ROLES)
                    & (
                        NetworkDevice.device_type.is_(None)
                        | NetworkDevice.device_type.in_(TOPOLOGY_DEFAULT_TYPES)
                    )
                )
                | NetworkDevice.device_type.in_(TOPOLOGY_DEFAULT_TYPES)
            )
            .order_by(NetworkDevice.name.asc())
            .limit(500)
        )
        if pop_site_id:
            stmt = stmt.where(NetworkDevice.pop_site_id == coerce_uuid(pop_site_id))
        devices = list(db.scalars(stmt).all())

    allowed_device_ids = {dev.id for dev in devices}
    nodes = []
    for dev in devices:
        pop_name = dev.pop_site.name if dev.pop_site else "Unassigned"
        location_parts = [part for part in [getattr(dev.pop_site, "city", None), getattr(dev.pop_site, "region", None)] if part]
        nodes.append({
            "id": str(dev.id),
            "name": dev.name or str(dev.id)[:8],
            "status": dev.status.value if dev.status else "unknown",
            "device_type": str(dev.device_type or ""),
            "vendor": str(dev.vendor or ""),
            "ip": str(dev.mgmt_ip or dev.hostname or ""),
            "pop_site_id": str(dev.pop_site_id) if dev.pop_site_id else "",
            "pop_site_name": pop_name,
            "location_label": ", ".join(location_parts) if location_parts else pop_name,
        })

    # Build edge list
    edges = []
    bundles: dict[str, list[str]] = {}
    for link in links:
        if allowed_device_ids and (
            link.source_device_id not in allowed_device_ids or link.target_device_id not in allowed_device_ids
        ):
            continue
        edge = _link_to_edge(db, link, include_utilization=include_utilization)
        edges.append(edge)
        if link.bundle_key:
            bundles.setdefault(link.bundle_key, []).append(str(link.id))

    site_summaries: dict[str, dict] = {}
    for node in nodes:
        site_key = node["pop_site_name"] or "Unassigned"
        entry = site_summaries.setdefault(
            site_key,
            {
                "pop_site_name": site_key,
                "location_label": node["location_label"] or site_key,
                "node_count": 0,
            },
        )
        entry["node_count"] += 1

    return {
        "nodes": nodes,
        "edges": edges,
        "bundles": bundles,
        "site_summaries": sorted(site_summaries.values(), key=lambda item: (-item["node_count"], item["pop_site_name"])),
        "stats": {
            "node_count": len(nodes),
            "edge_count": len(edges),
            "bundle_count": len(bundles),
            "site_count": len(site_summaries),
        },
    }


def _link_to_edge(
    db: Session,
    link: NetworkTopologyLink,
    *,
    include_utilization: bool = True,
) -> dict:
    """Convert a topology link to a D3-friendly edge dict."""
    src_iface_name = link.source_interface.name if link.source_interface else None
    tgt_iface_name = link.target_interface.name if link.target_interface else None

    edge: dict = {
        "id": str(link.id),
        "source": str(link.source_device_id),
        "target": str(link.target_device_id),
        "source_device": link.source_device.name if link.source_device else "",
        "target_device": link.target_device.name if link.target_device else "",
        "source_interface": src_iface_name or "",
        "target_interface": tgt_iface_name or "",
        "role": link.link_role.value if link.link_role else "unknown",
        "medium": link.medium.value if link.medium else "unknown",
        "capacity_bps": link.capacity_bps,
        "bundle_key": link.bundle_key,
        "topology_group": link.topology_group,
        "admin_status": link.admin_status.value if link.admin_status else "enabled",
        "notes": link.notes or "",
    }

    if include_utilization:
        util = compute_link_utilization(db, link)
        edge.update(util)

    return edge


# ── Utilization ──────────────────────────────────────────────────────


def compute_link_utilization(db: Session, link: NetworkTopologyLink) -> dict:
    """Compute current link utilization from interface metrics.

    Returns dict with rx_bps, tx_bps, utilization_pct, util_state.
    Falls back to "unknown" when telemetry is missing.
    """
    rx_bps: float | None = None
    tx_bps: float | None = None

    # Try source interface metrics first
    for iface_id in (link.source_interface_id, link.target_interface_id):
        if not iface_id:
            continue
        rx_metric = db.scalars(
            select(DeviceMetric.value)
            .where(
                DeviceMetric.interface_id == iface_id,
                DeviceMetric.metric_type == MetricType.rx_bps,
            )
            .order_by(DeviceMetric.recorded_at.desc())
            .limit(1)
        ).first()
        tx_metric = db.scalars(
            select(DeviceMetric.value)
            .where(
                DeviceMetric.interface_id == iface_id,
                DeviceMetric.metric_type == MetricType.tx_bps,
            )
            .order_by(DeviceMetric.recorded_at.desc())
            .limit(1)
        ).first()
        if rx_metric is not None:
            rx_bps = float(rx_metric)
        if tx_metric is not None:
            tx_bps = float(tx_metric)
        if rx_bps is not None or tx_bps is not None:
            break  # Got data from one interface, don't double-count

    if rx_bps is None and tx_bps is None:
        return {
            "rx_bps": None,
            "tx_bps": None,
            "utilization_pct": None,
            "util_state": "unknown",
        }

    total_bps = (rx_bps or 0) + (tx_bps or 0)
    capacity = link.capacity_bps or 0

    if capacity > 0:
        util_pct = round((total_bps / capacity) * 100, 1)
    else:
        util_pct = None

    # Classify utilization state
    if util_pct is None:
        util_state = "unknown"
    elif util_pct >= 90:
        util_state = "critical"
    elif util_pct >= 70:
        util_state = "warning"
    elif util_pct >= 40:
        util_state = "moderate"
    else:
        util_state = "normal"

    return {
        "rx_bps": rx_bps,
        "tx_bps": tx_bps,
        "utilization_pct": util_pct,
        "util_state": util_state,
    }


# ── Bundle Helpers ───────────────────────────────────────────────────


def detect_parallel_links(db: Session) -> list[dict]:
    """Find device pairs with multiple links between them."""
    stmt = (
        select(
            NetworkTopologyLink.source_device_id,
            NetworkTopologyLink.target_device_id,
            func.count(NetworkTopologyLink.id).label("link_count"),
        )
        .where(NetworkTopologyLink.is_active.is_(True))
        .group_by(
            NetworkTopologyLink.source_device_id,
            NetworkTopologyLink.target_device_id,
        )
        .having(func.count(NetworkTopologyLink.id) > 1)
    )
    rows = db.execute(stmt).all()
    return [
        {
            "source_device_id": str(r[0]),
            "target_device_id": str(r[1]),
            "link_count": r[2],
        }
        for r in rows
    ]


def get_device_links(db: Session, device_id: str) -> list[NetworkTopologyLink]:
    """Get all active links connected to a device (as source or target)."""
    uid = coerce_uuid(device_id)
    return list(
        db.scalars(
            select(NetworkTopologyLink)
            .options(
                joinedload(NetworkTopologyLink.source_device),
                joinedload(NetworkTopologyLink.target_device),
                joinedload(NetworkTopologyLink.source_interface),
                joinedload(NetworkTopologyLink.target_interface),
            )
            .where(
                NetworkTopologyLink.is_active.is_(True),
                (
                    (NetworkTopologyLink.source_device_id == uid)
                    | (NetworkTopologyLink.target_device_id == uid)
                ),
            )
        ).unique().all()
    )


def node_summary(db: Session, device_id: str) -> dict:
    """Build a summary for a single node including its links and health."""
    device = db.get(NetworkDevice, coerce_uuid(device_id))
    if not device:
        raise HTTPException(status_code=404, detail="Device not found")

    links = get_device_links(db, device_id)
    interfaces = get_device_interfaces(db, device_id)

    return {
        "device": {
            "id": str(device.id),
            "name": device.name,
            "status": device.status.value if device.status else "unknown",
            "ip": str(device.mgmt_ip or device.hostname or ""),
            "vendor": str(device.vendor or ""),
            "snmp_enabled": bool(device.snmp_enabled),
            "last_snmp_ok": bool(device.last_snmp_ok) if device.last_snmp_ok is not None else None,
            "last_snmp_at": device.last_snmp_at.isoformat() if device.last_snmp_at else None,
            "pop_site_name": device.pop_site.name if device.pop_site else "Unassigned",
            "location_label": ", ".join(
                [part for part in [getattr(device.pop_site, "city", None), getattr(device.pop_site, "region", None)] if part]
            ) if device.pop_site else "Unassigned",
        },
        "links": [_link_to_edge(db, link) for link in links],
        "interfaces": interfaces,
        "link_count": len(links),
        "interface_count": len(interfaces),
    }


# ── Form Helpers ─────────────────────────────────────────────────────


def get_form_options(db: Session) -> dict:
    """Return dropdown options for link create/edit forms."""
    devices = list(
        db.scalars(
            select(NetworkDevice)
            .where(NetworkDevice.is_active.is_(True))
            .order_by(NetworkDevice.name.asc())
            .limit(200)
        ).all()
    )
    return {
        "devices": [{"id": str(d.id), "name": d.name or str(d.id)[:8]} for d in devices],
        "link_roles": [r.value for r in TopologyLinkRole],
        "mediums": [m.value for m in TopologyLinkMedium],
        "admin_statuses": [s.value for s in TopologyLinkAdminStatus],
        "topology_groups": _get_topology_groups(db),
        "pop_sites": _get_pop_sites(db),
    }


def get_device_interfaces(db: Session, device_id: str) -> list[dict]:
    """Return interfaces for a device (for AJAX dropdown population)."""
    ifaces = list(
        db.scalars(
            select(DeviceInterface)
            .where(DeviceInterface.device_id == coerce_uuid(device_id))
            .order_by(DeviceInterface.snmp_index.asc().nulls_last(), DeviceInterface.name.asc())
        ).all()
    )
    return [
        {
            "id": str(i.id),
            "name": i.name or str(i.id)[:8],
            "status": i.status.value if hasattr(i.status, "value") else (i.status or ""),
            "speed_mbps": i.speed_mbps,
            "monitored": bool(i.monitored),
        }
        for i in ifaces
    ]


def _get_topology_groups(db: Session) -> list[str]:
    """Get distinct topology group names."""
    rows = db.scalars(
        select(NetworkTopologyLink.topology_group)
        .where(NetworkTopologyLink.topology_group.isnot(None))
        .distinct()
    ).all()
    return sorted([r for r in rows if r])


def _get_pop_sites(db: Session) -> list[dict]:
    rows = list(
        db.scalars(
            select(PopSite)
            .where(PopSite.is_active.is_(True))
            .order_by(PopSite.name.asc())
        ).all()
    )
    return [{"id": str(site.id), "name": site.name} for site in rows]


# Singleton
topology_links = TopologyLinks()
