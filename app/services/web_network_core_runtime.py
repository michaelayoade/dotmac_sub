"""Runtime helpers for admin core-device monitoring actions."""

from __future__ import annotations

import logging
import os
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from datetime import UTC, datetime

from sqlalchemy import and_, func, select
from sqlalchemy.orm import Session

from app.models.network_monitoring import (
    DeviceInterface,
    DeviceMetric,
    DeviceStatus,
    MetricType,
    NetworkDevice,
)
from app.services import ping as ping_service
from app.services.db_session_adapter import db_session_adapter

logger = logging.getLogger(__name__)

# DeviceStatus mixes poller-observed states (online/offline/degraded) with the
# operator-set admin state (maintenance) in one column. Pollers must go through
# this gate so a device an operator put into maintenance is never silently
# flipped back online/offline/degraded by a health check.
_OBSERVED_DEVICE_STATUSES = frozenset(
    {DeviceStatus.online, DeviceStatus.offline, DeviceStatus.degraded}
)


def set_device_observed_status(device: NetworkDevice, observed: DeviceStatus) -> bool:
    """Apply a poller-observed device status. Returns True if it changed.

    Refuses to overwrite the admin ``maintenance`` state — that's an operator
    override the pollers do not own. Same-status writes are a no-op.
    """
    if device.status == DeviceStatus.maintenance:
        return False
    if device.status == observed:
        return False
    device.status = observed
    return True


def _maybe_trigger_olt_retry(
    db: Session, device: NetworkDevice, check_type: str
) -> None:
    """Trigger immediate OLT retry if this device is linked to an OLT.

    Only triggers on first failure (when down_since was just set).

    Args:
        db: Database session.
        device: The NetworkDevice that failed.
        check_type: "ping" or "snmp".
    """
    from sqlalchemy import select

    from app.models.network import OLTDevice

    # Only trigger on first failure (transition from healthy to failed)
    if check_type == "ping" and device.ping_down_since is None:
        return  # Not a new failure
    if check_type == "snmp" and device.snmp_down_since is None:
        return  # Not a new failure

    # Find linked OLT by matching IP, hostname, or name
    olt = None
    if device.mgmt_ip:
        olt = db.scalar(
            select(OLTDevice).where(
                OLTDevice.mgmt_ip == device.mgmt_ip,
                OLTDevice.is_active.is_(True),
            )
        )
    if olt is None and device.hostname:
        olt = db.scalar(
            select(OLTDevice).where(
                OLTDevice.hostname == device.hostname,
                OLTDevice.is_active.is_(True),
            )
        )
    if olt is None and device.name:
        olt = db.scalar(
            select(OLTDevice).where(
                OLTDevice.name == device.name,
                OLTDevice.is_active.is_(True),
            )
        )

    if olt:
        try:
            from app.tasks.olt_health_retry import trigger_immediate_retry

            logger.info(
                "Triggering immediate %s retry for OLT %s (device %s)",
                check_type,
                olt.name,
                device.name,
            )
            trigger_immediate_retry(str(olt.id), delay_seconds=10)
        except Exception as exc:
            logger.warning(
                "Failed to trigger immediate retry for OLT %s: %s",
                olt.name,
                exc,
            )


def _bounded_max_workers(max_workers: int) -> int:
    try:
        configured = int(os.getenv("NETWORK_MONITORING_MAX_WORKERS", "4"))
    except ValueError:
        configured = 4
    effective = configured or max_workers
    return max(1, min(effective, 12))


def _release_postgres_read_transaction(db: Session) -> None:
    """Release a read transaction only where long idle transactions hurt us.

    Test sessions commonly run inside a SQLite transaction fixture; rolling
    that back here erases fixture data and expires caller-visible ORM objects.
    The production issue this protects against is PostgreSQL-specific.
    """
    bind = db.get_bind()
    if bind.dialect.name.startswith("postgres"):
        db.rollback()


@dataclass
class PingResult:
    """Result of a core device ping probe."""

    success: bool
    device: NetworkDevice


def refresh_devices_health(
    db: Session,
    devices: list[NetworkDevice],
    *,
    include_snmp: bool = False,
    max_workers: int = 4,
) -> dict[str, int]:
    """Refresh ping and vendor-backed monitoring health for a list of devices.

    Runs lightweight reachability checks for the provided devices and returns
    summary counters.
    """
    _ = db  # Explicitly unused: each worker uses an isolated DB session.
    totals = {"checked": 0, "ping": 0, "snmp": 0}
    targets: list[tuple[str, bool, bool]] = []
    for device in devices:
        if not device.id:
            continue
        do_ping = bool(device.ping_enabled)
        do_snmp = bool(device.snmp_enabled) and include_snmp
        targets.append((str(device.id), do_ping, do_snmp))
        totals["checked"] += 1
        if do_ping:
            totals["ping"] += 1
        if do_snmp:
            totals["snmp"] += 1

    if not targets:
        return totals

    workers = max(1, min(_bounded_max_workers(max_workers), len(targets)))
    # The caller's session is only used to build the primitive target list.
    # Release the read transaction before the long ping/SNMP fan-out; each
    # worker opens and commits its own short-lived session.
    _release_postgres_read_transaction(db)
    pool = ThreadPoolExecutor(max_workers=workers)
    try:
        futures = [
            pool.submit(_refresh_device_health_worker, device_id, do_ping, do_snmp)
            for device_id, do_ping, do_snmp in targets
        ]
        for future in as_completed(futures):
            try:
                future.result()
            except Exception:
                logger.exception("Core-device refresh worker crashed unexpectedly.")
    finally:
        # cancel_futures: on an abnormal exit (Celery soft time limit) drop the
        # queued backlog and wait only for the <= `workers` in-flight probes. A
        # plain context-manager exit waits for the ENTIRE backlog, which turned
        # soft kills into hard kills mid-cleanup and leaked the poll task's
        # advisory lock into a pooled DB connection.
        pool.shutdown(wait=True, cancel_futures=True)
    return totals


def refresh_stale_devices_health(
    db: Session,
    devices: list[NetworkDevice],
    *,
    ping_interval_seconds: int,
    snmp_interval_seconds: int,
    include_snmp: bool = True,
    force: bool = False,
    max_workers: int = 4,
    max_devices: int | None = None,
) -> dict[str, int]:
    """Refresh only devices whose ping or vendor-backed monitoring checks are stale.

    `force=True` refreshes all eligible devices regardless of recency.
    `max_devices` caps one call to the N longest-unchecked stale devices so a
    scheduled sweep always fits its time budget; the next run picks up the rest.
    """
    now = datetime.now(UTC)
    ping_interval_seconds = max(10, int(ping_interval_seconds or 0))
    snmp_interval_seconds = max(30, int(snmp_interval_seconds or 0))

    stale_targets: list[NetworkDevice] = []
    for device in devices:
        if not device.id:
            continue

        ping_stale = False
        snmp_stale = False

        # A check that can never produce a result must not count as stale:
        # ping_device early-returns without stamping last_ping_at when there
        # is no mgmt_ip, so a ping-enabled hostname-only device stays "stale"
        # forever — and under a max_devices cap those permanently-stale
        # devices monopolise every batch and starve the real ones.
        if device.ping_enabled and device.mgmt_ip:
            if force or device.last_ping_at is None:
                ping_stale = True
            else:
                last_ping_at = device.last_ping_at
                if last_ping_at.tzinfo is None:
                    last_ping_at = last_ping_at.replace(tzinfo=UTC)
                ping_stale = (
                    now - last_ping_at
                ).total_seconds() >= ping_interval_seconds

        if include_snmp and device.snmp_enabled and (device.mgmt_ip or device.hostname):
            if force or device.last_snmp_at is None:
                snmp_stale = True
            else:
                last_snmp_at = device.last_snmp_at
                if last_snmp_at.tzinfo is None:
                    last_snmp_at = last_snmp_at.replace(tzinfo=UTC)
                snmp_stale = (
                    now - last_snmp_at
                ).total_seconds() >= snmp_interval_seconds

        if ping_stale or snmp_stale:
            stale_targets.append(device)

    if not stale_targets:
        return {"checked": 0, "ping": 0, "snmp": 0}
    if max_devices is not None and len(stale_targets) > max_devices:
        _epoch = datetime(1970, 1, 1, tzinfo=UTC)

        def _last_checked(device: NetworkDevice) -> datetime:
            checked = device.last_ping_at or device.last_snmp_at
            if checked is None:
                return _epoch
            return checked if checked.tzinfo else checked.replace(tzinfo=UTC)

        stale_targets.sort(key=_last_checked)
        stale_targets = stale_targets[:max_devices]
    return refresh_devices_health(
        db,
        stale_targets,
        include_snmp=include_snmp,
        max_workers=max_workers,
    )


def _refresh_device_health_worker(device_id: str, do_ping: bool, do_snmp: bool) -> None:
    """Refresh health for a single device in an isolated DB session."""
    db = db_session_adapter.create_session()
    try:
        device = get_device(db, device_id)
        if not device:
            return
        if do_ping:
            ping_device(db, device_id)
        if do_snmp:
            from app.services.network_vendor_polling import (
                refresh_device_from_vendor_api,
            )

            refresh_device_from_vendor_api(db, device)
        db.commit()
    except Exception:
        db.rollback()
        logger.exception("Health refresh failed for core device %s", device_id)
    finally:
        db.close()


def ping_device(
    db: Session, device_id: str
) -> tuple[NetworkDevice | None, str | None, bool]:
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
    ping_success, latency_ms = ping_service.run_ping(device.mgmt_ip, timeout_seconds=4)

    # Track if this is a new failure for immediate retry trigger
    was_healthy = device.ping_down_since is None

    device.last_ping_at = now
    device.last_ping_ok = ping_success
    delay_minutes = max(0, int(device.notification_delay_minutes or 0))
    if ping_success:
        device.ping_down_since = None
        # Keep degraded when SNMP is still down, otherwise mark online.
        if device.snmp_enabled and device.last_snmp_ok is False:
            set_device_observed_status(device, DeviceStatus.degraded)
        else:
            set_device_observed_status(device, DeviceStatus.online)
    else:
        if device.ping_down_since is None:
            device.ping_down_since = now
        if _delay_elapsed(device.ping_down_since, now, delay_minutes):
            set_device_observed_status(device, DeviceStatus.offline)
        # Trigger immediate retry if this is a new failure
        if was_healthy:
            _maybe_trigger_olt_retry(db, device, "ping")
    _record_ping_metric(
        db, device, now=now, success=ping_success, latency_ms=latency_ms
    )
    db.flush()
    _recompute_parent_rollup(db, device)
    db.flush()
    return device, None, ping_success


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
    metric_value = (
        int(round(latency_ms))
        if (success and latency_ms is not None)
        else (-1 if not success else 0)
    )
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
            set_device_observed_status(device, DeviceStatus.offline)
        elif device.status != DeviceStatus.offline:
            set_device_observed_status(device, DeviceStatus.degraded)
    db.flush()
    _recompute_parent_rollup(db, device)
    db.flush()


def _delay_elapsed(
    down_since: datetime | None, now: datetime, delay_minutes: int
) -> bool:
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
                set_device_observed_status(parent, DeviceStatus.degraded)
            elif (
                parent.status == DeviceStatus.degraded
                and parent.ping_down_since is None
                and (not parent.snmp_enabled or parent.snmp_down_since is None)
            ):
                set_device_observed_status(parent, DeviceStatus.online)
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
        interface_metrics_by_type = {
            metric.metric_type: metric for metric in interface_metrics
        }
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


def discover_interfaces_and_health(
    db: Session, device: NetworkDevice
) -> tuple[int, int]:
    """Direct interface discovery is disabled; monitoring ingestion owns this data."""
    db.flush()
    return 0, 0


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
        badge_class = (
            "bg-slate-100 text-slate-600 dark:bg-slate-700 dark:text-slate-300"
        )
        label = "Disabled"
    elif device.last_ping_ok:
        badge_class = (
            "bg-green-100 text-green-800 dark:bg-green-900/30 dark:text-green-400"
        )
        label = "OK"
    elif device.last_ping_at:
        badge_class = "bg-red-100 text-red-800 dark:bg-red-900/30 dark:text-red-400"
        label = "Failed"
    else:
        badge_class = (
            "bg-slate-100 text-slate-600 dark:bg-slate-700 dark:text-slate-300"
        )
        label = "Unknown"
    return (
        f'<span class="inline-flex items-center rounded-full px-3 py-1.5 text-sm font-medium {badge_class}">'
        f"Ping: {label}</span>"
    )


def render_snmp_badge(device: NetworkDevice) -> str:
    """Render HTML badge for device SNMP status."""
    if not device.snmp_enabled:
        badge_class = (
            "bg-slate-100 text-slate-600 dark:bg-slate-700 dark:text-slate-300"
        )
        label = "Disabled"
    elif device.last_snmp_ok:
        badge_class = (
            "bg-green-100 text-green-800 dark:bg-green-900/30 dark:text-green-400"
        )
        label = "OK"
    elif device.last_snmp_at:
        badge_class = "bg-red-100 text-red-800 dark:bg-red-900/30 dark:text-red-400"
        label = "Failed"
    else:
        badge_class = (
            "bg-slate-100 text-slate-600 dark:bg-slate-700 dark:text-slate-300"
        )
        label = "Unknown"
    return (
        f'<span class="inline-flex items-center rounded-full px-3 py-1.5 text-sm font-medium {badge_class}">'
        f"SNMP: {label}</span>"
    )


def render_device_health_content(device_health: dict[str, object]) -> str:
    """Render HTML content for device health panel."""
    last_seen = device_health.get("last_seen")
    last_seen_value = (
        last_seen.strftime("%b %d, %Y %H:%M")
        if isinstance(last_seen, datetime)
        else "--"
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
