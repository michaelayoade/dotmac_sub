"""Service helpers for admin network monitoring web routes."""

from __future__ import annotations

import logging
import os
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx
from sqlalchemy.orm import Session

logger = logging.getLogger(__name__)

_VICTORIAMETRICS_URL = os.getenv("VICTORIAMETRICS_URL", "http://victoriametrics:8428")


def monitoring_page_data(
    db: Session, *, format_duration, format_bps
) -> dict[str, object]:
    """Return payload for network monitoring dashboard.

    Delegates to the centralized core service method and adds
    bandwidth summary context.
    """
    from app.services.network_monitoring import NetworkDevices

    data = NetworkDevices.get_monitoring_dashboard_stats(
        db,
        format_duration=format_duration,
        format_bps=format_bps,
    )

    # Add bandwidth context
    data["bandwidth"] = _get_bandwidth_summary()
    data["nas_throughput"] = _get_nas_throughput_summary(db)

    # ONU status summary and PON outage detection (Sprint 2)
    from app.services.network_monitoring import (
        get_onu_status_summary,
        get_pon_outage_summary,
    )

    data["onu_summary"] = get_onu_status_summary(db)
    data["pon_outages"] = get_pon_outage_summary(db)

    # ONU status trend chart (Phase 4 — 24h time-series)
    data["onu_trend"] = _get_onu_status_trend(hours=24)

    # ONU authorization trend (Phase 6D — new registrations per day, 30 days)
    data["onu_auth_trend"] = _get_onu_auth_trend(db, days=30)

    # Network activity feed (Phase 6E — recent audit events)
    data["network_activity"] = _get_network_activity_feed(db, limit=15)

    return data


def alarms_page_data(
    db: Session, *, severity: str | None, status: str | None
) -> dict[str, object]:
    """Return payload for monitoring alarms page."""
    from app.models.network_monitoring import (
        Alert,
        AlertRule,
        AlertSeverity,
        AlertStatus,
    )

    alarms_query = db.query(Alert).order_by(Alert.triggered_at.desc())
    if status:
        try:
            alarms_query = alarms_query.filter(Alert.status == AlertStatus(status))
        except ValueError:
            pass
    else:
        alarms_query = alarms_query.filter(
            Alert.status.in_([AlertStatus.open, AlertStatus.acknowledged])
        )
    if severity:
        try:
            alarms_query = alarms_query.filter(
                Alert.severity == AlertSeverity(severity)
            )
        except ValueError:
            pass
    alarms = alarms_query.limit(100).all()
    rules = (
        db.query(AlertRule)
        .filter(AlertRule.is_active.is_(True))
        .order_by(AlertRule.name)
        .all()
    )
    stats = {
        "critical": sum(
            1
            for a in alarms
            if a.severity == AlertSeverity.critical and a.status == AlertStatus.open
        ),
        "warning": sum(
            1
            for a in alarms
            if a.severity == AlertSeverity.warning and a.status == AlertStatus.open
        ),
        "info": sum(
            1
            for a in alarms
            if a.severity == AlertSeverity.info and a.status == AlertStatus.open
        ),
        "total_open": sum(1 for a in alarms if a.status == AlertStatus.open),
    }
    return {
        "alarms": alarms,
        "rules": rules,
        "stats": stats,
        "severity": severity,
        "status": status,
    }


def _vm_instant_query(query: str) -> list[dict[str, Any]]:
    """Execute a synchronous PromQL instant query against VictoriaMetrics.

    Returns an empty list on any error so callers always get a safe value.
    """
    try:
        resp = httpx.get(
            f"{_VICTORIAMETRICS_URL}/api/v1/query",
            params={"query": query},
            timeout=10,
        )
        resp.raise_for_status()
        data = resp.json()
        if isinstance(data, dict) and data.get("status") == "success":
            result = data.get("data", {}).get("result", [])
            if isinstance(result, list):
                return result
    except Exception as exc:
        logger.debug("VictoriaMetrics query failed (%s): %s", query[:60], exc)
    return []


def _vm_range_query(
    query: str,
    start: datetime,
    end: datetime,
    step: str,
) -> list[dict[str, Any]]:
    """Execute a synchronous PromQL range query against VictoriaMetrics.

    Returns an empty list on any error so callers always get a safe value.
    """
    try:
        resp = httpx.get(
            f"{_VICTORIAMETRICS_URL}/api/v1/query_range",
            params={
                "query": query,
                "start": start.isoformat(),
                "end": end.isoformat(),
                "step": step,
            },
            timeout=15,
        )
        resp.raise_for_status()
        data = resp.json()
        if isinstance(data, dict) and data.get("status") == "success":
            result = data.get("data", {}).get("result", [])
            if isinstance(result, list):
                return result
    except Exception as exc:
        logger.debug("VictoriaMetrics range query failed (%s): %s", query[:60], exc)
    return []


def _get_onu_status_trend(hours: int = 24) -> dict[str, Any]:
    """Query ONU status time-series from VictoriaMetrics.

    Returns dict with timestamps and series data for online, offline,
    and low-signal ONT counts over the given time period.
    """
    now = datetime.now(UTC)
    start = now - timedelta(hours=hours)
    step = "5m" if hours <= 24 else "30m"

    online_results = _vm_range_query(
        'onu_status_total{status="online"}', start, now, step
    )
    offline_results = _vm_range_query(
        'onu_status_total{status="offline"}', start, now, step
    )
    low_signal_results = _vm_range_query(
        'sum(onu_signal_low)', start, now, step
    )

    def _extract_series(results: list[dict[str, Any]]) -> dict[str, list]:
        """Extract timestamps and values from a range query result."""
        if not results:
            return {"timestamps": [], "values": []}
        values_raw = results[0].get("values", [])
        timestamps: list[str] = []
        values: list[float] = []
        for ts, val in values_raw:
            dt = datetime.fromtimestamp(float(ts), tz=UTC)
            timestamps.append(dt.strftime("%Y-%m-%dT%H:%M:%SZ"))
            try:
                values.append(float(val))
            except (ValueError, TypeError):
                values.append(0.0)
        return {"timestamps": timestamps, "values": values}

    online = _extract_series(online_results)
    offline = _extract_series(offline_results)
    low_signal = _extract_series(low_signal_results)

    has_data = bool(online["values"] or offline["values"] or low_signal["values"])

    return {
        "timestamps": online["timestamps"] or offline["timestamps"] or low_signal["timestamps"],
        "online": online["values"],
        "offline": offline["values"],
        "low_signal": low_signal["values"],
        "has_data": has_data,
    }


def _format_bps_simple(bps: float) -> str:
    """Human-readable bps formatting."""
    if bps >= 1_000_000_000:
        return f"{bps / 1_000_000_000:.2f} Gbps"
    if bps >= 1_000_000:
        return f"{bps / 1_000_000:.1f} Mbps"
    if bps >= 1_000:
        return f"{bps / 1_000:.0f} Kbps"
    return f"{bps:.0f} bps"


def _get_bandwidth_summary() -> dict[str, Any]:
    """Fetch aggregate bandwidth summary from VictoriaMetrics.

    Returns a dict with total_rx, total_tx (formatted), top_users list,
    and has_data boolean.  Gracefully returns zeros when VM has no data.
    """
    # Total aggregate throughput
    rx_results = _vm_instant_query("sum(bandwidth_rx_bps)")
    tx_results = _vm_instant_query("sum(bandwidth_tx_bps)")

    total_rx = 0.0
    total_tx = 0.0
    if rx_results and rx_results[0].get("value"):
        total_rx = float(rx_results[0]["value"][1])
    if tx_results and tx_results[0].get("value"):
        total_tx = float(tx_results[0]["value"][1])

    has_data = total_rx > 0 or total_tx > 0

    # Top 5 consumers
    top_users: list[dict[str, Any]] = []
    if has_data:
        top_query = (
            "topk(5, sum by (subscription_id) (bandwidth_rx_bps + bandwidth_tx_bps))"
        )
        top_results = _vm_instant_query(top_query)
        for r in top_results:
            metric = r.get("metric", {})
            value = r.get("value", [0, 0])
            total_bps = float(value[1]) if len(value) > 1 else 0.0
            top_users.append(
                {
                    "subscription_id": metric.get("subscription_id", "unknown"),
                    "total_bps": total_bps,
                    "total_formatted": _format_bps_simple(total_bps),
                }
            )

    return {
        "total_rx": total_rx,
        "total_tx": total_tx,
        "total_rx_formatted": _format_bps_simple(total_rx),
        "total_tx_formatted": _format_bps_simple(total_tx),
        "top_users": top_users,
        "has_data": has_data,
    }


def _get_nas_throughput_summary(db: Session) -> dict[str, Any]:
    """Fetch per-NAS throughput summary.

    Cross-references NAS IPs from VictoriaMetrics with the nas_devices
    table for friendly names.
    """
    from app.models.catalog import NasDevice

    # Per-NAS aggregate
    rx_results = _vm_instant_query("sum by (nas_device_id) (bandwidth_rx_bps)")
    tx_results = _vm_instant_query("sum by (nas_device_id) (bandwidth_tx_bps)")

    has_data = bool(rx_results) or bool(tx_results)

    # Build lookup of rx/tx by nas_device_id
    nas_rx: dict[str, float] = {}
    nas_tx: dict[str, float] = {}
    all_nas_ids: set[str] = set()
    for r in rx_results:
        nas_id = r.get("metric", {}).get("nas_device_id", "")
        val = float(r.get("value", [0, 0])[1]) if r.get("value") else 0.0
        nas_rx[nas_id] = val
        all_nas_ids.add(nas_id)
    for r in tx_results:
        nas_id = r.get("metric", {}).get("nas_device_id", "")
        val = float(r.get("value", [0, 0])[1]) if r.get("value") else 0.0
        nas_tx[nas_id] = val
        all_nas_ids.add(nas_id)

    # Resolve NAS names from DB
    nas_names: dict[str, str] = {}
    if all_nas_ids:
        devices = db.query(NasDevice).filter(NasDevice.id.in_(list(all_nas_ids))).all()
        for d in devices:
            nas_names[str(d.id)] = d.name or str(d.id)[:8]

    items: list[dict[str, Any]] = []
    for nas_id in sorted(all_nas_ids):
        rx = nas_rx.get(nas_id, 0.0)
        tx = nas_tx.get(nas_id, 0.0)
        items.append(
            {
                "nas_id": nas_id,
                "name": nas_names.get(nas_id, nas_id[:8] if nas_id else "Unknown"),
                "rx_bps": rx,
                "tx_bps": tx,
                "rx_formatted": _format_bps_simple(rx),
                "tx_formatted": _format_bps_simple(tx),
            }
        )

    # Sort by total throughput descending
    items.sort(key=lambda x: x["rx_bps"] + x["tx_bps"], reverse=True)

    return {
        "items": items,
        "has_data": has_data,
    }


def _get_onu_auth_trend(db: Session, days: int = 30) -> dict[str, Any]:
    """Count new ONT registrations per day for a bar chart.

    Queries OntUnit.created_at grouped by date for the last N days.
    """
    from sqlalchemy import cast, func
    from sqlalchemy.types import Date

    from app.models.network import OntUnit

    cutoff = datetime.now(UTC) - timedelta(days=days)

    rows = (
        db.query(
            cast(OntUnit.created_at, Date).label("day"),
            func.count().label("cnt"),
        )
        .filter(OntUnit.created_at >= cutoff)
        .group_by("day")
        .order_by("day")
        .all()
    )

    labels: list[str] = []
    values: list[int] = []
    for row in rows:
        labels.append(row.day.isoformat() if row.day else "")
        values.append(row.cnt)

    return {
        "labels": labels,
        "values": values,
        "has_data": bool(values),
    }


# Network entity types for the activity feed
_NETWORK_ENTITY_TYPES = (
    "olt",
    "ont",
    "pon_port",
    "vlan",
    "ip_pool",
    "ip_block",
    "network_device",
    "network_zone",
    "core_device",
    "nas",
    "splitter",
    "fdh_cabinet",
    "pop_site",
)

# Action → human-readable label
_ACTION_DISPLAY: dict[str, str] = {
    "create": "Created",
    "update": "Updated",
    "delete": "Deleted",
    "reboot": "Rebooted",
    "factory_reset": "Factory Reset",
    "set_wifi_ssid": "WiFi SSID Changed",
    "set_wifi_password": "WiFi Password Changed",
    "toggle_lan_port": "LAN Port Toggled",
    "assign": "Assigned",
    "unassign": "Unassigned",
    "activate": "Activated",
    "deactivate": "Deactivated",
}


def _get_network_activity_feed(
    db: Session, limit: int = 15
) -> list[dict[str, Any]]:
    """Fetch recent audit events for network-related entities."""
    from app.models.audit import AuditEvent

    events = (
        db.query(AuditEvent)
        .filter(AuditEvent.entity_type.in_(_NETWORK_ENTITY_TYPES))
        .order_by(AuditEvent.occurred_at.desc())
        .limit(limit)
        .all()
    )

    items: list[dict[str, Any]] = []
    for ev in events:
        action_label = _ACTION_DISPLAY.get(ev.action, ev.action.replace("_", " ").title())
        items.append({
            "title": f"{ev.entity_type.replace('_', ' ').title()} {action_label}",
            "entity_type": ev.entity_type,
            "entity_id": ev.entity_id,
            "action": ev.action,
            "occurred_at": ev.occurred_at,
            "actor_type": ev.actor_type.value if ev.actor_type else "system",
            "is_success": ev.is_success,
        })

    return items
