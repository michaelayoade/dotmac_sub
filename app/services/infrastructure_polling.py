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
        totals.update(push_interface_counters(db))
    except Exception:  # counters are additive; never fail the health sweep
        logger.exception("interface_counter_push_failed")
    return totals


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
