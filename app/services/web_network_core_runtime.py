"""Runtime helpers for admin core-device monitoring actions."""

from __future__ import annotations

import ipaddress
import logging
import re
import subprocess
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime

from sqlalchemy import and_, func, select
from sqlalchemy.orm import Session

from app.models.network_monitoring import (
    DeviceInterface,
    DeviceMetric,
    DeviceStatus,
    MetricType,
    NetworkDeviceSnmpOid,
    NetworkDevice,
)

logger = logging.getLogger(__name__)


@dataclass
class PingResult:
    """Result of a core device ping probe."""

    success: bool
    device: NetworkDevice


def _is_ipv6_host(host: str) -> bool:
    """Check if host is an IPv6 address."""
    try:
        return ipaddress.ip_address(host).version == 6
    except ValueError:
        return False


def _build_ping_command(host: str) -> list[str]:
    """Build a ping command for the given host."""
    command = ["ping", "-c", "1", "-W", "2", host]
    if _is_ipv6_host(host):
        command.insert(1, "-6")
    return command


def ping_device(db: Session, device_id: str) -> tuple[NetworkDevice | None, str | None, bool]:
    """Run a ping probe against a core device and persist the result.

    Returns (device, error_message, ping_success).
    """
    device = get_device(db, device_id)
    if not device:
        return None, "Device not found.", False

    if not device.mgmt_ip:
        return device, "Management IP is missing.", False

    ping_success = False
    latency_ms: float | None = None
    now = datetime.now(UTC)
    try:
        result = subprocess.run(
            _build_ping_command(device.mgmt_ip),
            capture_output=True,
            text=True,
            check=False,
            timeout=4,
        )
        ping_success = result.returncode == 0
        if ping_success:
            latency_ms = _extract_latency_ms(result.stdout)
    except Exception:
        ping_success = False

    device.last_ping_at = now
    device.last_ping_ok = ping_success
    delay_minutes = max(0, int(device.notification_delay_minutes or 0))
    if ping_success:
        device.ping_down_since = None
        # Keep degraded when SNMP is still down, otherwise mark online.
        if device.snmp_enabled and device.last_snmp_ok is False:
            device.status = DeviceStatus.degraded
        else:
            device.status = DeviceStatus.online
    else:
        if device.ping_down_since is None:
            device.ping_down_since = now
        if _delay_elapsed(device.ping_down_since, now, delay_minutes):
            device.status = DeviceStatus.offline
    _record_ping_metric(db, device, now=now, success=ping_success, latency_ms=latency_ms)
    db.flush()
    _recompute_parent_rollup(db, device)
    db.flush()
    return device, None, ping_success


def _extract_latency_ms(output: str) -> float | None:
    """Extract ping latency in milliseconds from ping output."""
    match = re.search(r"time[=<]\s*([0-9.]+)\s*ms", output or "")
    if not match:
        return None
    try:
        return float(match.group(1))
    except ValueError:
        return None


def _record_ping_metric(
    db: Session,
    device: NetworkDevice,
    *,
    now: datetime,
    success: bool,
    latency_ms: float | None,
) -> None:
    """Store ping status history for trend/last-N badges.

    `MetricType.custom` with unit=`ping_ms` records successful latency;
    failures use value `-1` with unit=`ping_timeout`.
    """
    if device.id is None:
        return
    metric_value = int(round(latency_ms)) if (success and latency_ms is not None) else (-1 if not success else 0)
    metric_unit = "ping_ms" if success else "ping_timeout"
    db.add(
        DeviceMetric(
            device_id=device.id,
            interface_id=None,
            metric_type=MetricType.custom,
            value=metric_value,
            unit=metric_unit,
            recorded_at=now,
        )
    )


def snmp_check_device(db: Session, device_id: str) -> tuple[NetworkDevice | None, str | None]:
    """Run SNMP uptime check against a core device and persist the result.

    Returns (device, error_message).
    """
    device = get_device(db, device_id)
    if not device:
        return None, "Device not found."

    now = datetime.now(UTC)
    delay_minutes = max(0, int(device.notification_delay_minutes or 0))
    if not device.snmp_enabled:
        return device, None

    if not device.mgmt_ip and not device.hostname:
        device.last_snmp_at = now
        device.last_snmp_ok = False
        if device.snmp_down_since is None:
            device.snmp_down_since = now
        if _delay_elapsed(device.snmp_down_since, now, delay_minutes):
            if device.ping_enabled and device.last_ping_ok is False:
                device.status = DeviceStatus.offline
            elif device.status != DeviceStatus.offline:
                device.status = DeviceStatus.degraded
        db.flush()
        _recompute_parent_rollup(db, device)
        db.flush()
        return device, None

    try:
        from app.services.snmp_discovery import _run_snmpwalk

        _run_snmpwalk(device, ".1.3.6.1.2.1.1.3.0", timeout=8)
        device.last_snmp_at = now
        device.last_snmp_ok = True
        device.snmp_down_since = None
        if device.status == DeviceStatus.degraded and (
            not device.ping_enabled or device.last_ping_ok is not False
        ):
            device.status = DeviceStatus.online
        db.flush()
        _recompute_parent_rollup(db, device)
        db.flush()
    except Exception:
        device.last_snmp_at = now
        device.last_snmp_ok = False
        if device.snmp_down_since is None:
            device.snmp_down_since = now
        if _delay_elapsed(device.snmp_down_since, now, delay_minutes):
            if device.ping_enabled and device.last_ping_ok is False:
                device.status = DeviceStatus.offline
            elif device.status != DeviceStatus.offline:
                device.status = DeviceStatus.degraded
        db.flush()
        _recompute_parent_rollup(db, device)
        db.flush()

    return device, None


@dataclass
class SnmpDebugResult:
    """Result of SNMP debug walk."""

    device: NetworkDevice
    error: str | None = None
    output: str | None = None


def snmp_debug_device(db: Session, device_id: str) -> SnmpDebugResult:
    """Run SNMP debug walk and return interface data."""
    device = get_device(db, device_id)
    if not device:
        return SnmpDebugResult(device=NetworkDevice(), error="Device not found.")

    if not device.snmp_enabled:
        return SnmpDebugResult(device=device, error="SNMP is disabled for this device.")

    if not device.mgmt_ip and not device.hostname:
        return SnmpDebugResult(
            device=device, error="Management IP or hostname is required for SNMP."
        )

    try:
        from app.services.snmp_discovery import _run_snmpbulkwalk

        descr_lines = _run_snmpbulkwalk(device, ".1.3.6.1.2.1.2.2.1.2")[:20]
        status_lines = _run_snmpbulkwalk(device, ".1.3.6.1.2.1.2.2.1.8")[:20]
        alias_lines = _run_snmpbulkwalk(device, ".1.3.6.1.2.1.31.1.1.1.18")[:20]
    except Exception as exc:
        return SnmpDebugResult(device=device, error=f"SNMP debug failed: {exc!s}")

    output = "\n".join(
        [
            "ifDescr:",
            *descr_lines,
            "",
            "ifOperStatus:",
            *status_lines,
            "",
            "ifAlias:",
            *alias_lines,
        ]
    ).strip()
    return SnmpDebugResult(device=device, output=output)


def mark_discovery_failure(db: Session, device: NetworkDevice) -> None:
    """Mark device SNMP as failed and flush."""
    now = datetime.now(UTC)
    delay_minutes = max(0, int(device.notification_delay_minutes or 0))
    device.last_snmp_at = now
    device.last_snmp_ok = False
    if device.snmp_down_since is None:
        device.snmp_down_since = now
    if _delay_elapsed(device.snmp_down_since, now, delay_minutes):
        if device.ping_enabled and device.last_ping_ok is False:
            device.status = DeviceStatus.offline
        elif device.status != DeviceStatus.offline:
            device.status = DeviceStatus.degraded
    db.flush()
    _recompute_parent_rollup(db, device)
    db.flush()


def _delay_elapsed(down_since: datetime | None, now: datetime, delay_minutes: int) -> bool:
    if delay_minutes <= 0:
        return True
    if down_since is None:
        return False
    if down_since.tzinfo is None:
        down_since = down_since.replace(tzinfo=UTC)
    if now.tzinfo is None:
        now = now.replace(tzinfo=UTC)
    return (now - down_since).total_seconds() >= (delay_minutes * 60)


def _recompute_parent_rollup(db: Session, device: NetworkDevice) -> None:
    """Propagate child impact statuses up the parent chain."""
    parent_id = device.parent_device_id
    if parent_id is None and device.id is not None:
        parent_id = db.scalars(
            select(NetworkDevice.parent_device_id).where(NetworkDevice.id == device.id)
        ).first()
    visited_ids: set[object] = set()
    while parent_id and parent_id not in visited_ids:
        parent = db.get(NetworkDevice, parent_id)
        if not parent:
            break
        visited_ids.add(parent.id)
        child_statuses = list(
            db.scalars(
                select(NetworkDevice.status)
                .where(NetworkDevice.parent_device_id == parent.id)
                .where(NetworkDevice.is_active.is_(True))
            ).all()
        )
        has_child_impact = any(
            status in {DeviceStatus.offline, DeviceStatus.degraded}
            for status in child_statuses
        )
        if parent.status != DeviceStatus.offline:
            if has_child_impact:
                parent.status = DeviceStatus.degraded
            elif (
                parent.status == DeviceStatus.degraded
                and parent.ping_down_since is None
                and (not parent.snmp_enabled or parent.snmp_down_since is None)
            ):
                parent.status = DeviceStatus.online
        parent_id = parent.parent_device_id


def get_device(db: Session, device_id: str) -> NetworkDevice | None:
    """Get core device by id."""
    return db.scalars(
        select(NetworkDevice).where(NetworkDevice.id == device_id)
    ).first()


def compute_health(
    db: Session,
    device: NetworkDevice,
    *,
    interface_id: str | None,
    format_duration: Callable[[float | int | None], str],
    format_bps: Callable[[float | int | None], str],
) -> dict[str, object]:
    """Compute current health summary for a device/interface."""
    selected_interface = None
    if interface_id:
        selected_interface = db.scalars(
            select(DeviceInterface)
            .where(
                DeviceInterface.id == interface_id,
                DeviceInterface.device_id == device.id,
            )
            .where(DeviceInterface.name.ilike("eth%"))
        ).first()

    metric_types = [MetricType.cpu, MetricType.memory, MetricType.uptime]
    if not selected_interface:
        metric_types.extend([MetricType.rx_bps, MetricType.tx_bps])
    latest_metrics_subq = (
        select(
            DeviceMetric.metric_type,
            func.max(DeviceMetric.recorded_at).label("latest"),
        )
        .where(DeviceMetric.device_id == device.id)
        .where(DeviceMetric.metric_type.in_(metric_types))
        .group_by(DeviceMetric.metric_type)
        .subquery()
    )
    latest_metrics = db.scalars(
        select(DeviceMetric)
        .join(
            latest_metrics_subq,
            and_(
                DeviceMetric.metric_type == latest_metrics_subq.c.metric_type,
                DeviceMetric.recorded_at == latest_metrics_subq.c.latest,
            ),
        )
        .where(DeviceMetric.device_id == device.id)
    ).all()
    metrics_by_type = {metric.metric_type: metric for metric in latest_metrics}
    cpu_metric = metrics_by_type.get(MetricType.cpu)
    mem_metric = metrics_by_type.get(MetricType.memory)
    uptime_metric = metrics_by_type.get(MetricType.uptime)

    if selected_interface:
        interface_metric_types = [MetricType.rx_bps, MetricType.tx_bps]
        interface_metrics_subq = (
            select(
                DeviceMetric.metric_type,
                func.max(DeviceMetric.recorded_at).label("latest"),
            )
            .where(DeviceMetric.device_id == device.id)
            .where(DeviceMetric.interface_id == selected_interface.id)
            .where(DeviceMetric.metric_type.in_(interface_metric_types))
            .group_by(DeviceMetric.metric_type)
            .subquery()
        )
        interface_metrics = db.scalars(
            select(DeviceMetric)
            .join(
                interface_metrics_subq,
                and_(
                    DeviceMetric.metric_type == interface_metrics_subq.c.metric_type,
                    DeviceMetric.recorded_at == interface_metrics_subq.c.latest,
                ),
            )
            .where(DeviceMetric.device_id == device.id)
            .where(DeviceMetric.interface_id == selected_interface.id)
        ).all()
        interface_metrics_by_type = {metric.metric_type: metric for metric in interface_metrics}
        rx_metric = interface_metrics_by_type.get(MetricType.rx_bps)
        tx_metric = interface_metrics_by_type.get(MetricType.tx_bps)
    else:
        rx_metric = metrics_by_type.get(MetricType.rx_bps)
        tx_metric = metrics_by_type.get(MetricType.tx_bps)

    return {
        "cpu": f"{cpu_metric.value:.1f}%" if cpu_metric else "--",
        "memory": f"{mem_metric.value:.1f}%" if mem_metric else "--",
        "uptime": format_duration(uptime_metric.value if uptime_metric else None),
        "rx": format_bps(rx_metric.value) if rx_metric else "--",
        "tx": format_bps(tx_metric.value) if tx_metric else "--",
        "last_seen": device.last_ping_at or device.last_snmp_at,
    }


def discover_interfaces_and_health(db: Session, device: NetworkDevice) -> tuple[int, int]:
    """Run SNMP discovery, persist interfaces + health metrics."""
    from app.services.snmp_discovery import (
        apply_interface_snapshot,
        collect_device_health,
        collect_interface_snapshot,
    )

    snapshots = collect_interface_snapshot(device)
    created, updated = apply_interface_snapshot(db, device, snapshots, create_missing=True)
    _ensure_interface_traffic_oids(db, device, snapshots)
    health = collect_device_health(device)
    recorded_at = datetime.now(UTC)
    cpu_percent = health.get("cpu_percent")
    if cpu_percent is not None:
        db.add(
            DeviceMetric(
                device_id=device.id,
                metric_type=MetricType.cpu,
                value=int(cpu_percent),
                unit="percent",
                recorded_at=recorded_at,
            )
        )
    memory_percent = health.get("memory_percent")
    if memory_percent is not None:
        db.add(
            DeviceMetric(
                device_id=device.id,
                metric_type=MetricType.memory,
                value=int(memory_percent),
                unit="percent",
                recorded_at=recorded_at,
            )
        )
    uptime_seconds = health.get("uptime_seconds")
    if uptime_seconds is not None:
        db.add(
            DeviceMetric(
                device_id=device.id,
                metric_type=MetricType.uptime,
                value=int(uptime_seconds),
                unit="seconds",
                recorded_at=recorded_at,
            )
        )
    rx_bps = health.get("rx_bps")
    if rx_bps is not None:
        db.add(
            DeviceMetric(
                device_id=device.id,
                metric_type=MetricType.rx_bps,
                value=int(rx_bps),
                unit="bps",
                recorded_at=recorded_at,
            )
        )
    tx_bps = health.get("tx_bps")
    if tx_bps is not None:
        db.add(
            DeviceMetric(
                device_id=device.id,
                metric_type=MetricType.tx_bps,
                value=int(tx_bps),
                unit="bps",
                recorded_at=recorded_at,
            )
        )
    device.last_snmp_at = datetime.now(UTC)
    device.last_snmp_ok = True
    db.flush()
    return created, updated


def _ensure_interface_traffic_oids(db: Session, device: NetworkDevice, snapshots: list[object]) -> None:
    """Ensure interface-level in/out traffic OIDs exist for discovered interfaces."""
    if device.id is None:
        return
    existing = set(
        db.scalars(
            select(NetworkDeviceSnmpOid.oid).where(NetworkDeviceSnmpOid.device_id == device.id)
        ).all()
    )
    for snapshot in snapshots:
        idx = getattr(snapshot, "index", None)
        name = getattr(snapshot, "name", None) or "if"
        if not idx:
            continue
        in_oid = f"1.3.6.1.2.1.31.1.1.1.6.{idx}"
        out_oid = f"1.3.6.1.2.1.31.1.1.1.10.{idx}"
        if in_oid not in existing:
            db.add(
                NetworkDeviceSnmpOid(
                    device_id=device.id,
                    title=f"{name} in",
                    oid=in_oid,
                    check_interval_seconds=60,
                    rrd_data_source_type="counter",
                    is_enabled=True,
                )
            )
            existing.add(in_oid)
        if out_oid not in existing:
            db.add(
                NetworkDeviceSnmpOid(
                    device_id=device.id,
                    title=f"{name} out",
                    oid=out_oid,
                    check_interval_seconds=60,
                    rrd_data_source_type="counter",
                    is_enabled=True,
                )
            )
            existing.add(out_oid)


def render_device_status_badge(status_value: str) -> str:
    """Render HTML badge for device status."""
    if status_value == "online":
        return (
            '<span class="inline-flex items-center gap-1.5 rounded-full px-3 py-1.5 text-sm font-medium '
            'bg-green-100 text-green-800 dark:bg-green-900/30 dark:text-green-400">'
            '<span class="h-2 w-2 rounded-full bg-green-500"></span>'
            "Online</span>"
        )
    if status_value == "offline":
        return (
            '<span class="inline-flex items-center gap-1.5 rounded-full px-3 py-1.5 text-sm font-medium '
            'bg-red-100 text-red-800 dark:bg-red-900/30 dark:text-red-400">'
            '<span class="h-2 w-2 rounded-full bg-red-500"></span>'
            "Offline</span>"
        )
    if status_value == "degraded":
        return (
            '<span class="inline-flex items-center gap-1.5 rounded-full px-3 py-1.5 text-sm font-medium '
            'bg-amber-100 text-amber-800 dark:bg-amber-900/30 dark:text-amber-400">'
            '<span class="h-2 w-2 rounded-full bg-amber-500"></span>'
            "Degraded</span>"
        )
    return (
        '<span class="inline-flex items-center gap-1.5 rounded-full px-3 py-1.5 text-sm font-medium '
        'bg-blue-100 text-blue-800 dark:bg-blue-900/30 dark:text-blue-400">'
        '<span class="h-2 w-2 rounded-full bg-blue-500"></span>'
        "Maintenance</span>"
    )


def render_ping_badge(device: NetworkDevice) -> str:
    """Render HTML badge for device ping status."""
    if not device.ping_enabled:
        badge_class = "bg-slate-100 text-slate-600 dark:bg-slate-700 dark:text-slate-300"
        label = "Disabled"
    elif device.last_ping_ok:
        badge_class = "bg-green-100 text-green-800 dark:bg-green-900/30 dark:text-green-400"
        label = "OK"
    elif device.last_ping_at:
        badge_class = "bg-red-100 text-red-800 dark:bg-red-900/30 dark:text-red-400"
        label = "Failed"
    else:
        badge_class = "bg-slate-100 text-slate-600 dark:bg-slate-700 dark:text-slate-300"
        label = "Unknown"
    return (
        f'<span class="inline-flex items-center rounded-full px-3 py-1.5 text-sm font-medium {badge_class}">'
        f"Ping: {label}</span>"
    )


def render_snmp_badge(device: NetworkDevice) -> str:
    """Render HTML badge for device SNMP status."""
    if not device.snmp_enabled:
        badge_class = "bg-slate-100 text-slate-600 dark:bg-slate-700 dark:text-slate-300"
        label = "Disabled"
    elif device.last_snmp_ok:
        badge_class = "bg-green-100 text-green-800 dark:bg-green-900/30 dark:text-green-400"
        label = "OK"
    elif device.last_snmp_at:
        badge_class = "bg-red-100 text-red-800 dark:bg-red-900/30 dark:text-red-400"
        label = "Failed"
    else:
        badge_class = "bg-slate-100 text-slate-600 dark:bg-slate-700 dark:text-slate-300"
        label = "Unknown"
    return (
        f'<span class="inline-flex items-center rounded-full px-3 py-1.5 text-sm font-medium {badge_class}">'
        f"SNMP: {label}</span>"
    )


def render_device_health_content(device_health: dict[str, object]) -> str:
    """Render HTML content for device health panel."""
    last_seen = device_health.get("last_seen")
    last_seen_value = (
        last_seen.strftime("%b %d, %Y %H:%M") if isinstance(last_seen, datetime) else "--"
    )
    return (
        '<div class="grid grid-cols-1 gap-4 sm:grid-cols-2">'
        "<div>"
        '<p class="text-xs font-medium uppercase text-slate-500 dark:text-slate-400">CPU</p>'
        f'<p class="mt-1 text-sm font-semibold text-slate-900 dark:text-white">{device_health.get("cpu", "--")}</p>'
        "</div>"
        "<div>"
        '<p class="text-xs font-medium uppercase text-slate-500 dark:text-slate-400">Memory</p>'
        f'<p class="mt-1 text-sm font-semibold text-slate-900 dark:text-white">{device_health.get("memory", "--")}</p>'
        "</div>"
        "<div>"
        '<p class="text-xs font-medium uppercase text-slate-500 dark:text-slate-400">Uptime</p>'
        f'<p class="mt-1 text-sm font-semibold text-slate-900 dark:text-white">{device_health.get("uptime", "--")}</p>'
        "</div>"
        "<div>"
        '<p class="text-xs font-medium uppercase text-slate-500 dark:text-slate-400">Last Seen</p>'
        f'<p class="mt-1 text-sm font-semibold text-slate-900 dark:text-white">{last_seen_value}</p>'
        "</div>"
        "<div>"
        '<p class="text-xs font-medium uppercase text-slate-500 dark:text-slate-400">Rx</p>'
        f'<p class="mt-1 text-sm font-semibold text-slate-900 dark:text-white">{device_health.get("rx", "--")}</p>'
        "</div>"
        "<div>"
        '<p class="text-xs font-medium uppercase text-slate-500 dark:text-slate-400">Tx</p>'
        f'<p class="mt-1 text-sm font-semibold text-slate-900 dark:text-white">{device_health.get("tx", "--")}</p>'
        "</div>"
        "</div>"
    )


def format_duration(seconds: float | int | None) -> str:
    """Format seconds into a human-readable duration string."""
    if seconds is None:
        return "--"
    total = int(seconds)
    days, rem = divmod(total, 86400)
    hours, rem = divmod(rem, 3600)
    minutes, _ = divmod(rem, 60)
    if days > 0:
        return f"{days}d {hours}h"
    if hours > 0:
        return f"{hours}h {minutes}m"
    return f"{minutes}m"


def format_bps(value: float | int | None) -> str:
    """Format bits per second into a human-readable string."""
    if value is None:
        return "--"
    units = ["bps", "Kbps", "Mbps", "Gbps", "Tbps"]
    size = float(value)
    unit_index = 0
    while size >= 1000 and unit_index < len(units) - 1:
        size /= 1000.0
        unit_index += 1
    return f"{size:.1f} {units[unit_index]}"


def coerce_float_or_none(value: object) -> float | None:
    """Coerce a value to float or return None."""
    if value is None:
        return None
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str):
        s = value.strip()
        if not s:
            return None
        try:
            return float(s)
        except ValueError:
            return None
    return None
