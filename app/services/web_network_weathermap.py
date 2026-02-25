"""Service helpers for admin network weathermap routes."""

from __future__ import annotations

import os
from collections import defaultdict

from sqlalchemy import and_, func
from sqlalchemy.orm import Session

from app.models.network_monitoring import DeviceMetric, MetricType, NetworkDevice

_WARN_BPS = int(os.getenv("WEATHERMAP_WARN_BPS", "100000000"))
_HIGH_BPS = int(os.getenv("WEATHERMAP_HIGH_BPS", "500000000"))


def _link_state(total_bps: float | None) -> str:
    if total_bps is None:
        return "unknown"
    if total_bps >= _HIGH_BPS:
        return "high"
    if total_bps >= _WARN_BPS:
        return "moderate"
    return "low"


def build_weathermap_data(db: Session) -> dict[str, object]:
    devices = (
        db.query(NetworkDevice)
        .filter(NetworkDevice.is_active.is_(True))
        .order_by(NetworkDevice.name.asc())
        .all()
    )

    if not devices:
        return {
            "nodes": [],
            "links": [],
            "stats": {
                "nodes": 0,
                "links": 0,
                "low_links": 0,
                "moderate_links": 0,
                "high_links": 0,
                "unknown_links": 0,
            },
        }

    device_ids = [item.id for item in devices]

    latest_subq = (
        db.query(
            DeviceMetric.device_id.label("device_id"),
            DeviceMetric.metric_type.label("metric_type"),
            func.max(DeviceMetric.recorded_at).label("latest"),
        )
        .filter(DeviceMetric.device_id.in_(device_ids))
        .filter(DeviceMetric.metric_type.in_([MetricType.rx_bps, MetricType.tx_bps]))
        .group_by(DeviceMetric.device_id, DeviceMetric.metric_type)
        .subquery()
    )

    metric_rows = (
        db.query(DeviceMetric)
        .join(
            latest_subq,
            and_(
                DeviceMetric.device_id == latest_subq.c.device_id,
                DeviceMetric.metric_type == latest_subq.c.metric_type,
                DeviceMetric.recorded_at == latest_subq.c.latest,
            ),
        )
        .all()
    )

    metrics_by_device: dict[str, dict[str, float]] = defaultdict(lambda: {"rx_bps": 0.0, "tx_bps": 0.0})
    for row in metric_rows:
        key = str(row.device_id)
        if row.metric_type == MetricType.rx_bps:
            metrics_by_device[key]["rx_bps"] = float(row.value or 0)
        elif row.metric_type == MetricType.tx_bps:
            metrics_by_device[key]["tx_bps"] = float(row.value or 0)

    by_id = {str(item.id): item for item in devices}
    children: dict[str, list[NetworkDevice]] = defaultdict(list)
    roots: list[NetworkDevice] = []

    for item in devices:
        if item.parent_device_id and str(item.parent_device_id) in by_id:
            children[str(item.parent_device_id)].append(item)
        else:
            roots.append(item)

    for key in children:
        children[key].sort(key=lambda x: x.name.lower())
    roots.sort(key=lambda x: x.name.lower())

    levels: dict[int, list[NetworkDevice]] = defaultdict(list)
    visited: set[str] = set()

    def _walk(node: NetworkDevice, depth: int) -> None:
        node_id = str(node.id)
        if node_id in visited:
            return
        visited.add(node_id)
        levels[depth].append(node)
        for child in children.get(node_id, []):
            _walk(child, depth + 1)

    for root in roots:
        _walk(root, 0)

    for item in devices:
        if str(item.id) not in visited:
            _walk(item, 0)

    nodes: list[dict[str, object]] = []
    pos_by_id: dict[str, tuple[int, int]] = {}

    x_gap = 270
    y_gap = 130

    for depth in sorted(levels.keys()):
        row = levels[depth]
        for idx, item in enumerate(row):
            x = 140 + (depth * x_gap)
            y = 100 + (idx * y_gap)
            item_id = str(item.id)
            pos_by_id[item_id] = (x, y)

            m = metrics_by_device.get(item_id, {"rx_bps": 0.0, "tx_bps": 0.0})
            total = float(m["rx_bps"] + m["tx_bps"])
            has_metrics = item_id in metrics_by_device
            state = _link_state(total if has_metrics else None)

            nodes.append(
                {
                    "id": item_id,
                    "name": item.name,
                    "x": x,
                    "y": y,
                    "role": item.role.value if item.role else None,
                    "status": item.status.value if item.status else "unknown",
                    "pop_site": item.pop_site.name if item.pop_site else None,
                    "rx_bps": m["rx_bps"],
                    "tx_bps": m["tx_bps"],
                    "total_bps": total,
                    "traffic_state": state,
                }
            )

    links: list[dict[str, object]] = []
    for item in devices:
        if not item.parent_device_id:
            continue
        source_id = str(item.parent_device_id)
        target_id = str(item.id)
        if source_id not in pos_by_id or target_id not in pos_by_id:
            continue
        m = metrics_by_device.get(target_id)
        total = float((m or {}).get("rx_bps", 0.0) + (m or {}).get("tx_bps", 0.0)) if m else None
        state = _link_state(total)
        sx, sy = pos_by_id[source_id]
        tx, ty = pos_by_id[target_id]
        links.append(
            {
                "source": source_id,
                "target": target_id,
                "sx": sx,
                "sy": sy,
                "tx": tx,
                "ty": ty,
                "total_bps": total,
                "state": state,
            }
        )

    stats = {
        "nodes": len(nodes),
        "links": len(links),
        "low_links": sum(1 for item in links if item["state"] == "low"),
        "moderate_links": sum(1 for item in links if item["state"] == "moderate"),
        "high_links": sum(1 for item in links if item["state"] == "high"),
        "unknown_links": sum(1 for item in links if item["state"] == "unknown"),
    }

    return {
        "nodes": nodes,
        "links": links,
        "stats": stats,
    }
