"""Native infrastructure polling sweep (Zabbix runtime cutover, Phase 1).

Periodically runs the same ping + SNMP / vendor-API reachability checks the
admin core-device pages run on demand (``web_network_core_runtime``), so
device health no longer depends on an operator opening a dashboard — or on
Zabbix. Results land on the per-device poll columns (``last_ping_*``,
``ping_down_since``, ``last_snmp_*``, ``snmp_down_since``, ``status``); the
topology live-status warmer (``topology.live_status``) derives the cached
``live_status`` the outage pipeline reads from those columns.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime

from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from app.models.network_monitoring import DeviceInterface, NetworkDevice
from app.services.web_network_core_runtime import refresh_stale_devices_health

logger = logging.getLogger(__name__)

# Advisory-lock key for the single-flight poll task ("niP").
ADVISORY_LOCK_KEY = 0x6E_69_50

# Per-check staleness windows: a device is re-probed only when its last check
# is older than these, so the beat cadence can be tighter than the probe rate.
DEFAULT_PING_INTERVAL_SECONDS = 60
DEFAULT_SNMP_INTERVAL_SECONDS = 300

# Cap one sweep to the N longest-unchecked stale devices. With 12 workers and
# worst-case ~4s per down device this bounds a run to ~2 minutes — safely
# inside the task's soft time limit even when the whole fleet is stale (fresh
# deploy, poller downtime); successive 60s beat runs cover the remainder.
DEFAULT_MAX_DEVICES_PER_RUN = 300

# Raw IF-MIB octet counters for admin-monitored interfaces, pushed every sweep.
# Counters (not rates): the reader (core_router_metrics) derives bps with
# rate() at query time, so the poller stays stateless — no previous-sample
# bookkeeping, and counter wraps/resets are VictoriaMetrics' problem.
INTERFACE_IN_OCTETS_METRIC = "core_interface_in_octets_total"
INTERFACE_OUT_OCTETS_METRIC = "core_interface_out_octets_total"

# Per-probe ping results as VM series (degradation detection before outage
# detection — rising latency shows up long before a device stops answering).
# Latency only exists for successful probes; loss is 1/0 per probe.
PING_LATENCY_METRIC = "device_ping_latency_ms"
PING_LOSS_METRIC = "device_ping_loss"

_vm_writer = None


def _writer():
    global _vm_writer
    if _vm_writer is None:
        from app.services.bandwidth_metrics_adapter import VictoriaMetricsWriter

        _vm_writer = VictoriaMetricsWriter()
    return _vm_writer


def pollable_device_criteria() -> tuple:
    """Filter criteria matching the devices the poll sweep covers.

    Shared with the topology live-status warmer so "which devices have native
    poll data" is defined exactly once.
    """
    return (
        NetworkDevice.is_active.is_(True),
        or_(
            NetworkDevice.ping_enabled.is_(True),
            NetworkDevice.snmp_enabled.is_(True),
        ),
        or_(
            NetworkDevice.mgmt_ip.isnot(None),
            NetworkDevice.hostname.isnot(None),
        ),
    )


def pollable_devices(db: Session) -> list[NetworkDevice]:
    """Active devices with at least one enabled check and a reachable address."""
    return list(
        db.scalars(select(NetworkDevice).where(*pollable_device_criteria())).all()
    )


def poll_infrastructure(
    db: Session,
    *,
    ping_interval_seconds: int = DEFAULT_PING_INTERVAL_SECONDS,
    snmp_interval_seconds: int = DEFAULT_SNMP_INTERVAL_SECONDS,
    max_workers: int = 12,
    force: bool = False,
    max_devices: int | None = DEFAULT_MAX_DEVICES_PER_RUN,
) -> dict[str, int]:
    """Run one reachability sweep over the pollable devices.

    Delegates to ``refresh_stale_devices_health`` — each probe runs in its own
    worker thread with an isolated, self-committing session, honours the
    ``maintenance`` operator override, and rolls status up the parent chain.
    One run probes at most ``max_devices`` (longest-unchecked first).
    """
    devices = pollable_devices(db)
    sweep_started = datetime.now(UTC)
    totals = refresh_stale_devices_health(
        db,
        devices,
        ping_interval_seconds=ping_interval_seconds,
        snmp_interval_seconds=snmp_interval_seconds,
        include_snmp=True,
        force=force,
        max_workers=max_workers,
        max_devices=max_devices,
    )
    totals["devices"] = len(devices)
    try:
        totals.update(push_ping_metrics(db, since=sweep_started))
    except Exception:  # metrics are additive; never fail the health sweep
        logger.exception("ping_metric_push_failed")
    try:
        totals.update(push_interface_counters(db))
    except Exception:  # counters are additive; never fail the health sweep
        logger.exception("interface_counter_push_failed")
    return totals


def push_ping_metrics(db: Session, *, since: datetime) -> dict[str, int]:
    """Push this sweep's ping probe results to VictoriaMetrics.

    Reads the DeviceMetric rows the probe workers persisted after ``since``
    (unit ``ping_ms`` for successes, ``ping_timeout`` for failures) and emits
    ``device_ping_latency_ms`` (successes only) plus ``device_ping_loss``
    (1/0 per probe). Labels carry the aggregation dimensions the dashboards
    slice by: device_id, device_role, pop_site_id, matched_device_type.
    """
    from app.models.network_monitoring import DeviceMetric

    rows = db.execute(
        select(DeviceMetric, NetworkDevice)
        .join(NetworkDevice, NetworkDevice.id == DeviceMetric.device_id)
        .where(
            DeviceMetric.unit.in_(("ping_ms", "ping_timeout")),
            DeviceMetric.recorded_at >= since,
        )
    ).all()
    if not rows:
        return {"ping_metric_lines": 0, "ping_metric_write_failed": 0}

    lines: list[str] = []
    for metric, device in rows:
        recorded_at = metric.recorded_at
        if recorded_at.tzinfo is None:
            recorded_at = recorded_at.replace(tzinfo=UTC)
        ts_ms = int(recorded_at.timestamp() * 1000)
        role = getattr(device.role, "value", device.role) or "unknown"
        labels = (
            f'device_id="{device.id}",device_role="{role}",'
            f'pop_site_id="{device.pop_site_id or ""}",'
            f'matched_device_type="{device.matched_device_type or ""}"'
        )
        success = metric.unit == "ping_ms"
        if success:
            lines.append(
                f"{PING_LATENCY_METRIC}{{{labels}}} {float(metric.value)} {ts_ms}"
            )
        lines.append(f"{PING_LOSS_METRIC}{{{labels}}} {0 if success else 1} {ts_ms}")

    write_result = _writer().write_prometheus_lines(
        lines,
        adapter="infrastructure.polling",
        operation="ping_metrics",
    )
    return {
        "ping_metric_lines": len(lines),
        "ping_metric_write_failed": 0 if write_result.success else len(lines),
    }


def monitored_interface_targets(
    db: Session,
) -> list[tuple[NetworkDevice, list[DeviceInterface]]]:
    """SNMP-enabled active devices with admin-monitored, indexed interfaces.

    Devices whose last ping failed are skipped: they won't answer SNMP either,
    and each one would cost a full snmpget timeout per OID chunk.
    """
    rows = db.execute(
        select(NetworkDevice, DeviceInterface)
        .join(DeviceInterface, DeviceInterface.device_id == NetworkDevice.id)
        .where(
            NetworkDevice.is_active.is_(True),
            NetworkDevice.snmp_enabled.is_(True),
            NetworkDevice.last_ping_ok.isnot(False),
            DeviceInterface.monitored.is_(True),
            DeviceInterface.snmp_index.isnot(None),
        )
    ).all()
    by_device: dict = {}
    for device, iface in rows:
        by_device.setdefault(device.id, (device, []))[1].append(iface)
    return list(by_device.values())


def push_interface_counters(
    db: Session, *, now: datetime | None = None
) -> dict[str, int]:
    """Read IF-MIB octet counters for monitored interfaces, push to VictoriaMetrics.

    Feeds the admin live interface-bandwidth panel (``core_router_metrics``),
    which used to read these values from Zabbix items.
    """
    from app.services.snmp_probe import fetch_interface_octets

    targets = monitored_interface_targets(db)
    if not targets:
        return {
            "interface_devices": 0,
            "interface_lines": 0,
            "interface_write_failed": 0,
        }

    ts_ms = int((now or datetime.now(UTC)).timestamp() * 1000)
    lines: list[str] = []
    devices_read = 0
    for device, ifaces in targets:
        readings = fetch_interface_octets(
            device, [int(i.snmp_index) for i in ifaces if i.snmp_index is not None]
        )
        if not readings:
            continue
        devices_read += 1
        for iface in ifaces:
            if iface.snmp_index is None:
                continue
            reading = readings.get(int(iface.snmp_index))
            if reading is None:
                continue
            labels = (
                f'device_id="{device.id}",interface_id="{iface.id}",'
                f'snmp_index="{iface.snmp_index}"'
            )
            if reading.in_octets is not None:
                lines.append(
                    f"{INTERFACE_IN_OCTETS_METRIC}{{{labels}}} "
                    f"{reading.in_octets} {ts_ms}"
                )
            if reading.out_octets is not None:
                lines.append(
                    f"{INTERFACE_OUT_OCTETS_METRIC}{{{labels}}} "
                    f"{reading.out_octets} {ts_ms}"
                )
    write_failed = 0
    if lines:
        write_result = _writer().write_prometheus_lines(
            lines,
            adapter="infrastructure.polling",
            operation="interface_counters",
        )
        # The writer already logs and bumps the VM failure counter; surface
        # the failure in the task result too so ops can see it in task output.
        if not write_result.success:
            write_failed = len(lines)
    return {
        "interface_devices": devices_read,
        "interface_lines": len(lines),
        "interface_write_failed": write_failed,
    }


# ---------------------------------------------------------------------------
# Heartbeat: the poll task records every outcome so the admin-alert evaluator
# can notice silence. Zabbix's most important job was noticing when nothing
# was being measured; this is its replacement's dead-man switch.
# ---------------------------------------------------------------------------

HEARTBEAT_KEY = "infrastructure_poll:last_success"
SKIP_STREAK_KEY = "infrastructure_poll:skip_streak"
_HEARTBEAT_TTL_SECONDS = 7 * 86_400


def record_poll_success(result: dict, *, now: datetime | None = None) -> None:
    """Stamp the heartbeat after a completed sweep (advisory, cache-only)."""
    try:
        from app.services.app_cache import set_json

        stamp = {
            "at": (now or datetime.now(UTC)).isoformat(),
            "result": {k: v for k, v in result.items() if isinstance(v, int)},
        }
        set_json(HEARTBEAT_KEY, stamp, _HEARTBEAT_TTL_SECONDS)
        set_json(SKIP_STREAK_KEY, 0, _HEARTBEAT_TTL_SECONDS)
    except Exception:  # cache is advisory; never fail the sweep over it
        logger.exception("infrastructure_poll_heartbeat_write_failed")


def record_poll_skip() -> int:
    """Count consecutive already_running skips; returns the current streak."""
    try:
        from app.services.app_cache import get_json, set_json

        streak = int(get_json(SKIP_STREAK_KEY) or 0) + 1
        set_json(SKIP_STREAK_KEY, streak, _HEARTBEAT_TTL_SECONDS)
        return streak
    except Exception:
        logger.exception("infrastructure_poll_skip_streak_write_failed")
        return 0


def poll_health_snapshot(db: Session, *, now: datetime | None = None) -> dict:
    """Everything the alert evaluator needs to judge poll health.

    Returns plain scalars (no ORM objects):
    ``last_success_age_seconds`` (None = no heartbeat recorded),
    ``skip_streak``, ``interface_write_failed`` (from the last completed run),
    ``newest_ping_age_seconds`` (None = no pingable device ever stamped),
    ``pingable_devices``, ``poll_interval_seconds``.
    """
    from sqlalchemy import and_, func

    now = now or datetime.now(UTC)

    last_success_age: float | None = None
    interface_write_failed = 0
    skip_streak = 0
    try:
        from app.services.app_cache import get_json

        heartbeat = get_json(HEARTBEAT_KEY)
        if isinstance(heartbeat, dict) and heartbeat.get("at"):
            recorded = datetime.fromisoformat(str(heartbeat["at"]))
            if recorded.tzinfo is None:
                recorded = recorded.replace(tzinfo=UTC)
            last_success_age = max(0.0, (now - recorded).total_seconds())
            result = heartbeat.get("result") or {}
            interface_write_failed = int(result.get("interface_write_failed") or 0)
        skip_streak = int(get_json(SKIP_STREAK_KEY) or 0)
    except Exception:
        logger.exception("infrastructure_poll_heartbeat_read_failed")

    pingable = and_(
        NetworkDevice.is_active.is_(True),
        NetworkDevice.ping_enabled.is_(True),
        NetworkDevice.mgmt_ip.isnot(None),
    )
    newest_ping_at, pingable_devices = db.execute(
        select(func.max(NetworkDevice.last_ping_at), func.count()).where(pingable)
    ).one()
    newest_ping_age: float | None = None
    if newest_ping_at is not None:
        if newest_ping_at.tzinfo is None:
            newest_ping_at = newest_ping_at.replace(tzinfo=UTC)
        newest_ping_age = max(0.0, (now - newest_ping_at).total_seconds())

    try:
        from app.models.domain_settings import SettingDomain
        from app.services.settings_spec import resolve_value

        poll_interval = int(
            resolve_value(
                db,
                SettingDomain.network_monitoring,
                "infrastructure_poll_interval_seconds",
            )
            or 60
        )
    except Exception:
        poll_interval = 60

    return {
        "last_success_age_seconds": last_success_age,
        "skip_streak": skip_streak,
        "interface_write_failed": interface_write_failed,
        "newest_ping_age_seconds": newest_ping_age,
        "pingable_devices": int(pingable_devices or 0),
        "poll_interval_seconds": max(30, poll_interval),
    }
