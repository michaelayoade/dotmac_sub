"""Device monitoring metric collection and VictoriaMetrics push services.

Handles:
- Custom SNMP OID polling per device
- Interface traffic counter collection (ifHCInOctets/ifHCOutOctets)
- Pushing device/ONU metrics to VictoriaMetrics
- Subscriber impact counting on device outages
- Device metrics TTL cleanup
"""

from __future__ import annotations

import logging
import os
from datetime import UTC, datetime, timedelta

import httpx
from sqlalchemy import delete, func, select
from sqlalchemy.orm import Session

from app.models.network_monitoring import (
    DeviceInterface,
    DeviceMetric,
    MetricType,
    NetworkDevice,
    NetworkDeviceSnmpOid,
)

logger = logging.getLogger(__name__)

_VICTORIAMETRICS_URL = os.getenv("VICTORIAMETRICS_URL", "http://victoriametrics:8428")

# SNMP OIDs for 64-bit interface traffic counters (IF-MIB)
_IF_HC_IN_OCTETS = ".1.3.6.1.2.1.31.1.1.1.6"
_IF_HC_OUT_OCTETS = ".1.3.6.1.2.1.31.1.1.1.10"


# ── Custom SNMP OID Polling ──────────────────────────────────────────────


def poll_custom_snmp_oids(db: Session, device: NetworkDevice) -> dict[str, int]:
    """Poll all enabled custom SNMP OIDs for a device.

    Returns:
        {polled, updated, errors}
    """
    oids = list(
        db.scalars(
            select(NetworkDeviceSnmpOid)
            .where(
                NetworkDeviceSnmpOid.device_id == device.id,
                NetworkDeviceSnmpOid.is_enabled.is_(True),
            )
        ).all()
    )
    if not oids:
        return {"polled": 0, "updated": 0, "errors": 0}

    now = datetime.now(UTC)
    polled = 0
    updated = 0
    errors = 0

    for oid_config in oids:
        # Respect per-OID check interval
        if oid_config.last_polled_at:
            interval = oid_config.check_interval_seconds or 300
            next_poll = oid_config.last_polled_at + timedelta(seconds=interval)
            if now < next_poll:
                continue

        polled += 1
        try:
            value = _snmp_get_single(device, oid_config.oid)
            if value is not None:
                # Store as DeviceMetric
                metric = DeviceMetric(
                    device_id=device.id,
                    metric_type=MetricType.custom,
                    value=float(value),
                    unit=oid_config.title or oid_config.oid,
                    recorded_at=now,
                )
                db.add(metric)

                # Update OID tracking
                oid_config.last_polled_at = now
                oid_config.last_poll_status = "ok"
                oid_config.last_value = str(value)
                oid_config.last_error = None
                updated += 1
            else:
                oid_config.last_polled_at = now
                oid_config.last_poll_status = "no_response"
                oid_config.last_error = "No SNMP response"
                errors += 1
        except Exception as exc:
            oid_config.last_polled_at = now
            oid_config.last_poll_status = "error"
            oid_config.last_error = str(exc)[:200]
            errors += 1
            logger.warning(
                "Custom OID poll failed for device %s OID %s: %s",
                device.name,
                oid_config.oid,
                exc,
            )

    return {"polled": polled, "updated": updated, "errors": errors}


def _snmp_get_single(device: NetworkDevice, oid: str) -> float | None:
    """Perform a single SNMP GET on a device. Returns numeric value or None."""
    from app.services.snmp_client import snmp_get

    mgmt_ip = device.mgmt_ip
    community = device.snmp_community or "public"
    if not mgmt_ip:
        return None

    try:
        result = snmp_get(mgmt_ip, community, oid, timeout=8)
        if result is not None:
            return float(result)
    except (ValueError, TypeError):
        pass
    except Exception as exc:
        logger.debug("SNMP GET failed for %s %s: %s", mgmt_ip, oid, exc)
    return None


# ── Interface Traffic Counter Polling ────────────────────────────────────


def poll_interface_traffic(db: Session, device: NetworkDevice) -> dict[str, int]:
    """Poll 64-bit interface counters and compute bps deltas.

    Returns:
        {interfaces_polled, updated}
    """
    interfaces = list(
        db.scalars(
            select(DeviceInterface).where(
                DeviceInterface.device_id == device.id,
                DeviceInterface.status == "up",
            )
        ).all()
    )
    if not interfaces:
        return {"interfaces_polled": 0, "updated": 0}

    now = datetime.now(UTC)
    polled = 0
    updated = 0

    for iface in interfaces:
        polled += 1
        try:
            # Get ifIndex for this interface (stored as snmp_index or derived from name)
            if_index = getattr(iface, "snmp_index", None)
            if not if_index:
                continue

            in_oid = f"{_IF_HC_IN_OCTETS}.{if_index}"
            out_oid = f"{_IF_HC_OUT_OCTETS}.{if_index}"

            in_octets = _snmp_get_single(device, in_oid)
            out_octets = _snmp_get_single(device, out_oid)

            if in_octets is None or out_octets is None:
                continue

            # Calculate delta bps from previous values
            prev_in = getattr(iface, "last_in_octets", None)
            prev_out = getattr(iface, "last_out_octets", None)
            prev_ts = getattr(iface, "last_counter_at", None)

            if prev_in is not None and prev_out is not None and prev_ts:
                elapsed = (now - prev_ts).total_seconds()
                if elapsed > 0:
                    rx_bps = max(0, (in_octets - prev_in) * 8 / elapsed)
                    tx_bps = max(0, (out_octets - prev_out) * 8 / elapsed)

                    # Store metrics
                    for mt, val in [(MetricType.rx_bps, rx_bps), (MetricType.tx_bps, tx_bps)]:
                        db.add(DeviceMetric(
                            device_id=device.id,
                            interface_id=iface.id,
                            metric_type=mt,
                            value=val,
                            unit="bps",
                            recorded_at=now,
                        ))
                    updated += 1

            # Store current counters for next delta calculation
            iface.last_in_octets = in_octets  # type: ignore[assignment]
            iface.last_out_octets = out_octets  # type: ignore[assignment]
            iface.last_counter_at = now  # type: ignore[assignment]

        except Exception as exc:
            logger.debug("Interface traffic poll failed for %s/%s: %s", device.name, iface.name, exc)

    return {"interfaces_polled": polled, "updated": updated}


# ── VictoriaMetrics Push ─────────────────────────────────────────────────


def push_metrics_to_victoriametrics(metrics_lines: list[str]) -> bool:
    """Push Prometheus-format metric lines to VictoriaMetrics.

    Args:
        metrics_lines: List of Prometheus text format lines, e.g.:
            'cpu_usage{device_id="abc",device_name="Router1"} 42.5'

    Returns:
        True if push succeeded, False otherwise.
    """
    if not metrics_lines:
        return True

    payload = "\n".join(metrics_lines) + "\n"
    try:
        resp = httpx.post(
            f"{_VICTORIAMETRICS_URL}/api/v1/import/prometheus",
            content=payload,
            headers={"Content-Type": "text/plain"},
            timeout=10,
        )
        if resp.status_code in (200, 204):
            return True
        logger.warning("VictoriaMetrics push returned %d: %s", resp.status_code, resp.text[:200])
        return False
    except Exception as exc:
        logger.warning("VictoriaMetrics push failed: %s", exc)
        return False


def push_device_health_metrics(device: NetworkDevice, metrics: dict[str, float]) -> None:
    """Push a device's health metrics to VictoriaMetrics.

    Args:
        device: The network device.
        metrics: Dict of {metric_name: value}, e.g. {"cpu": 42.5, "memory": 65.2}
    """
    lines = []
    device_name = (device.name or "").replace('"', '\\"')
    for name, value in metrics.items():
        lines.append(
            f'device_{name}{{device_id="{device.id}",device_name="{device_name}"}} {value}'
        )
    push_metrics_to_victoriametrics(lines)


def push_onu_status_metrics(online: int, offline: int, low_signal: int) -> None:
    """Push ONU status counts to VictoriaMetrics."""
    lines = [
        f'onu_status_total{{status="online"}} {online}',
        f'onu_status_total{{status="offline"}} {offline}',
        f"onu_signal_low {low_signal}",
    ]
    push_metrics_to_victoriametrics(lines)


# ── Subscriber Impact ────────────────────────────────────────────────────


def count_affected_subscribers(db: Session, device: NetworkDevice) -> int:
    """Count subscribers potentially affected by a device outage.

    Checks:
    1. Active RADIUS accounting sessions with NAS IP matching device mgmt_ip
    2. ONT assignments on OLTs linked to this device
    """
    count = 0

    if device.mgmt_ip:
        # Count active RADIUS sessions on this NAS
        try:
            from app.models.radius import RadiusAccountingSession

            count = db.scalar(
                select(func.count())
                .select_from(RadiusAccountingSession)
                .where(
                    RadiusAccountingSession.nas_ip_address == device.mgmt_ip,
                    RadiusAccountingSession.stop_time.is_(None),
                )
            ) or 0
        except Exception:
            logger.debug("Could not count RADIUS sessions for device %s", device.name)

    return count


def update_device_subscriber_count(db: Session, device: NetworkDevice) -> int:
    """Update the subscriber count on a device and return the count."""
    count = count_affected_subscribers(db, device)
    device.current_subscriber_count = count
    return count


# ── Metrics Cleanup ──────────────────────────────────────────────────────


def cleanup_old_device_metrics(db: Session, retention_days: int = 90) -> int:
    """Delete device metrics older than retention_days.

    Deletes in batches to avoid long table locks.

    Returns:
        Total number of deleted records.
    """
    cutoff = datetime.now(UTC) - timedelta(days=retention_days)
    total_deleted = 0
    batch_size = 10_000

    while True:
        # Use a subquery to limit the delete to batch_size rows
        subq = (
            select(DeviceMetric.id)
            .where(DeviceMetric.recorded_at < cutoff)
            .limit(batch_size)
            .subquery()
        )
        result = db.execute(
            delete(DeviceMetric).where(DeviceMetric.id.in_(select(subq.c.id)))
        )
        deleted = result.rowcount
        db.commit()
        total_deleted += deleted

        if deleted < batch_size:
            break

    if total_deleted > 0:
        logger.info("Cleaned up %d device metrics older than %d days", total_deleted, retention_days)

    return total_deleted


# ── NAS → Monitoring Sync ────────────────────────────────────────────────

# Map NAS vendor to monitoring DeviceType
_VENDOR_TO_DEVICE_TYPE = {
    "mikrotik": "router",
    "huawei": "switch",
    "cisco": "router",
    "juniper": "router",
    "ubiquiti": "access_point",
    "cambium": "access_point",
    "nokia": "switch",
    "zte": "switch",
    "other": "other",
}


def sync_nas_to_monitoring(db: Session, nas_id: str) -> NetworkDevice:
    """Create or update a NetworkDevice record from a NasDevice.

    Links the NAS device to the monitoring system by:
    1. Creating a NetworkDevice if one doesn't exist
    2. Copying network config (IP, SNMP community, vendor, model)
    3. Enabling ping and SNMP monitoring
    4. Setting the NasDevice.network_device_id FK

    Returns the NetworkDevice record.
    """
    from app.models.catalog import NasDevice
    from app.models.network_monitoring import DeviceRole, DeviceType

    nas = db.get(NasDevice, nas_id)
    if not nas:
        raise ValueError(f"NAS device {nas_id} not found")

    # Check if already linked
    if nas.network_device_id:
        existing = db.get(NetworkDevice, nas.network_device_id)
        if existing:
            # Update fields from NAS
            _sync_nas_fields_to_device(nas, existing)
            db.flush()
            return existing

    # Check if a device already exists with this mgmt IP (dedup)
    mgmt_ip = nas.management_ip or nas.ip_address
    if mgmt_ip:
        existing = db.scalars(
            select(NetworkDevice).where(NetworkDevice.mgmt_ip == mgmt_ip)
        ).first()
        if existing:
            nas.network_device_id = existing.id
            _sync_nas_fields_to_device(nas, existing)
            db.flush()
            return existing

    # Create new NetworkDevice
    vendor_str = nas.vendor.value if nas.vendor else "other"
    device_type_str = _VENDOR_TO_DEVICE_TYPE.get(vendor_str, "other")

    device = NetworkDevice(
        name=nas.name,
        hostname=nas.name,
        mgmt_ip=mgmt_ip,
        vendor=nas.vendor.value if nas.vendor else None,
        model=nas.model,
        serial_number=nas.serial_number,
        device_type=DeviceType(device_type_str),
        role=DeviceRole.access,
        ping_enabled=True,
        snmp_enabled=bool(nas.snmp_community),
        snmp_community=nas.snmp_community,
        snmp_version=nas.snmp_version or "2c",
        snmp_port=nas.snmp_port or 161,
        pop_site_id=nas.pop_site_id,
        max_concurrent_subscribers=nas.max_concurrent_subscribers,
        notes=f"Auto-created from NAS device: {nas.name}",
    )
    db.add(device)
    db.flush()

    # Link back
    nas.network_device_id = device.id
    db.flush()

    logger.info("Created monitoring device %s from NAS %s (%s)", device.id, nas.name, mgmt_ip)
    return device


def _sync_nas_fields_to_device(nas, device: NetworkDevice) -> None:
    """Copy relevant fields from NAS to NetworkDevice."""
    device.name = nas.name
    device.mgmt_ip = nas.management_ip or nas.ip_address
    device.vendor = nas.vendor.value if nas.vendor else device.vendor
    device.model = nas.model or device.model
    device.serial_number = nas.serial_number or device.serial_number
    if nas.snmp_community and not device.snmp_community:
        device.snmp_community = nas.snmp_community
        device.snmp_enabled = True
    if nas.pop_site_id:
        device.pop_site_id = nas.pop_site_id
    if nas.max_concurrent_subscribers:
        device.max_concurrent_subscribers = nas.max_concurrent_subscribers


def sync_all_nas_to_monitoring(db: Session) -> dict[str, int]:
    """Sync all active NAS devices to the monitoring system.

    Returns:
        {synced, skipped, errors}
    """
    from app.models.catalog import NasDevice

    nas_devices = list(
        db.scalars(
            select(NasDevice).where(NasDevice.is_active.is_(True))
        ).all()
    )

    synced = 0
    skipped = 0
    errors = 0

    for nas in nas_devices:
        # Skip NAS devices without a usable IP
        mgmt_ip = nas.management_ip or nas.ip_address
        if not mgmt_ip:
            skipped += 1
            continue

        try:
            sync_nas_to_monitoring(db, str(nas.id))
            synced += 1
        except Exception as exc:
            db.rollback()
            errors += 1
            logger.warning("Failed to sync NAS %s to monitoring: %s", nas.name, exc)

    db.commit()
    logger.info("NAS monitoring sync complete: synced=%d skipped=%d errors=%d", synced, skipped, errors)
    return {"synced": synced, "skipped": skipped, "errors": errors}
