"""Vendor-specific polling adapters for network monitoring devices."""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from typing import Any, cast

from fastapi import HTTPException
from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from app.models.catalog import NasDevice, NasVendor
from app.models.network_monitoring import (
    DeviceMetric,
    DeviceStatus,
    MetricType,
    NetworkDevice,
)
from app.services import nas as nas_service
from app.services.snmp_probe import probe_snmp_reachability

logger = logging.getLogger(__name__)


def refresh_device_from_vendor_api(
    db: Session, device: NetworkDevice
) -> tuple[bool, bool]:
    """Refresh a device from vendor API when available.

    Returns `(handled, success)`.
    """
    vendor = str(device.vendor or "").lower()
    if "mikrotik" not in vendor:
        return refresh_device_from_direct_snmp(db, device)
    if not device.mgmt_ip:
        return refresh_device_from_direct_snmp(db, device)

    nas_device = db.scalars(
        select(NasDevice)
        .where(NasDevice.vendor == NasVendor.mikrotik)
        .where(NasDevice.is_active.is_(True))
        .where(
            or_(
                NasDevice.management_ip == device.mgmt_ip,
                NasDevice.ip_address == device.mgmt_ip,
            )
        )
    ).first()
    if not nas_device:
        return refresh_device_from_direct_snmp(db, device)

    now = datetime.now(UTC)
    try:
        status = nas_service.get_mikrotik_api_telemetry(nas_device, db=db)
    except HTTPException:
        device.last_snmp_at = now
        device.last_snmp_ok = False
        return True, False
    except Exception as exc:
        logger.error("Vendor API poll failed for device %s: %s", device.name, exc)
        device.last_snmp_at = now
        device.last_snmp_ok = False
        return True, False

    device.last_snmp_at = now
    device.last_snmp_ok = True
    # Route through the single gated writer so a maintenance device is never
    # flipped back online by a successful poll (see web_network_core_runtime).
    from app.services.web_network_core_runtime import set_device_observed_status

    set_device_observed_status(device, DeviceStatus.online)

    cpu_usage = status.get("cpu_usage")
    try:
        cpu_float = float(cast(Any, cpu_usage)) if cpu_usage is not None else None
    except (TypeError, ValueError):
        cpu_float = None
    if cpu_float is not None:
        db.add(
            DeviceMetric(
                device_id=device.id,
                metric_type=MetricType.cpu,
                value=int(round(cpu_float)),
                unit="percent",
                recorded_at=now,
            )
        )

    uptime_seconds = status.get("uptime_seconds")
    try:
        uptime_int = (
            int(cast(Any, uptime_seconds)) if uptime_seconds is not None else None
        )
    except (TypeError, ValueError):
        uptime_int = None
    if uptime_int is not None:
        db.add(
            DeviceMetric(
                device_id=device.id,
                metric_type=MetricType.uptime,
                value=uptime_int,
                unit="seconds",
                recorded_at=now,
            )
        )

    memory_percent = status.get("memory_percent")
    try:
        memory_float = (
            float(cast(Any, memory_percent)) if memory_percent is not None else None
        )
    except (TypeError, ValueError):
        memory_float = None
    if memory_float is not None:
        db.add(
            DeviceMetric(
                device_id=device.id,
                metric_type=MetricType.memory,
                value=int(round(memory_float)),
                unit="percent",
                recorded_at=now,
            )
        )

    rx_bps = status.get("rx_bps")
    tx_bps = status.get("tx_bps")
    try:
        rx_float = float(cast(Any, rx_bps)) if rx_bps is not None else None
    except (TypeError, ValueError):
        rx_float = None
    try:
        tx_float = float(cast(Any, tx_bps)) if tx_bps is not None else None
    except (TypeError, ValueError):
        tx_float = None
    if rx_float is not None:
        db.add(
            DeviceMetric(
                device_id=device.id,
                metric_type=MetricType.rx_bps,
                value=int(round(max(rx_float, 0.0))),
                unit="bps",
                recorded_at=now,
            )
        )
    if tx_float is not None:
        db.add(
            DeviceMetric(
                device_id=device.id,
                metric_type=MetricType.tx_bps,
                value=int(round(max(tx_float, 0.0))),
                unit="bps",
                recorded_at=now,
            )
        )

    active_subscribers = status.get("active_subscribers")
    try:
        active_int = (
            int(cast(Any, active_subscribers))
            if active_subscribers is not None
            else None
        )
    except (TypeError, ValueError):
        active_int = None
    if active_int is not None:
        device.current_subscriber_count = max(active_int, 0)

    return True, True


def refresh_device_from_direct_snmp(
    db: Session, device: NetworkDevice
) -> tuple[bool, bool]:
    """Refresh SNMP reachability by directly probing the device."""
    _ = db  # Reserved for future metric writes; keep the signature uniform.
    result = probe_snmp_reachability(device)
    if not result.handled:
        return False, False

    now = datetime.now(UTC)
    device.last_snmp_at = now
    device.last_snmp_ok = result.success

    if result.success:
        device.snmp_down_since = None
        from app.services.web_network_core_runtime import set_device_observed_status

        if not (device.ping_enabled and device.last_ping_ok is False):
            set_device_observed_status(device, DeviceStatus.online)
    else:
        if device.snmp_down_since is None:
            device.snmp_down_since = now

    return True, result.success
