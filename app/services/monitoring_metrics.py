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
    InterfaceStatus,
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
                oid_config.last_poll_status = "ok"
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
    from app.services.credential_crypto import decrypt_credential
    from app.services.snmp_client import snmp_get

    mgmt_ip = str(device.mgmt_ip) if device.mgmt_ip else None
    community = (
        decrypt_credential(device.snmp_community) if device.snmp_community else "public"
    ) or "public"
    if not mgmt_ip:
        return None

    # Use device's configured SNMP version (normalize to CLI format)
    raw_ver = str(getattr(device, "snmp_version", "") or "2c").strip().lower()
    version = "1" if raw_ver in ("1", "v1") else "2c"

    try:
        result = snmp_get(mgmt_ip, community, oid, timeout=8, version=version)
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
                DeviceInterface.status == InterfaceStatus.up,
                DeviceInterface.monitored.is_(True),
                DeviceInterface.snmp_index.is_not(None),
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
            iface.last_in_octets = in_octets
            iface.last_out_octets = out_octets
            iface.last_counter_at = now

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

    linked_nas = getattr(device, "nas_device", None)
    if linked_nas is not None:
        # Count active RADIUS sessions on this NAS
        try:
            from app.models.usage import AccountingStatus, RadiusAccountingSession

            count = db.scalar(
                select(func.count())
                .select_from(RadiusAccountingSession)
                .where(
                    RadiusAccountingSession.nas_device_id == linked_nas.id,
                    RadiusAccountingSession.session_end.is_(None),
                    RadiusAccountingSession.status_type != AccountingStatus.stop,
                )
            ) or 0
        except Exception as exc:
            logger.warning("Could not count RADIUS sessions for device %s: %s", device.name, exc)

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
            with db.begin_nested():  # savepoint — isolates per-NAS
                sync_nas_to_monitoring(db, str(nas.id))
            synced += 1
        except Exception as exc:
            errors += 1
            logger.warning("Failed to sync NAS %s to monitoring: %s", nas.name, exc)

    db.commit()
    logger.info("NAS monitoring sync complete: synced=%d skipped=%d errors=%d", synced, skipped, errors)
    return {"synced": synced, "skipped": skipped, "errors": errors}


# ── Vendor SNMP OID Mappings ────────────────────────────────────────


# Common SNMP OIDs for device system metrics by vendor
_VENDOR_HEALTH_OIDS: dict[str, dict[str, str]] = {
    "mikrotik": {
        "cpu": ".1.3.6.1.2.1.25.3.3.1.2.1",  # hrProcessorLoad
        "memory_total": ".1.3.6.1.2.1.25.2.3.1.5.65536",
        "memory_used": ".1.3.6.1.2.1.25.2.3.1.6.65536",
        "uptime": ".1.3.6.1.2.1.1.3.0",
        "temperature": ".1.3.6.1.4.1.14988.1.1.3.10.0",
    },
    "huawei": {
        "cpu": ".1.3.6.1.4.1.2011.5.25.31.1.1.1.1.5.67108873",
        "memory_total": ".1.3.6.1.4.1.2011.5.25.31.1.1.1.1.7.67108873",
        "memory_used": ".1.3.6.1.4.1.2011.5.25.31.1.1.1.1.8.67108873",
        "uptime": ".1.3.6.1.2.1.1.3.0",
        "temperature": ".1.3.6.1.4.1.2011.5.25.31.1.1.1.1.11.67108873",
    },
    "generic": {
        "cpu": ".1.3.6.1.2.1.25.3.3.1.2.1",  # HOST-RESOURCES-MIB
        "uptime": ".1.3.6.1.2.1.1.3.0",  # sysUpTime
    },
}

# Huawei OLT OIDs for ONT optical signal monitoring
_HUAWEI_ONT_SIGNAL_OID = ".1.3.6.1.4.1.2011.6.128.1.1.2.51.1.4"  # hwGponOntOpticalDdmRxPower


def poll_device_system_metrics(
    db: Session,
    device: NetworkDevice,
    *,
    vendor: str | None = None,
) -> dict[str, int]:
    """Poll CPU, memory, temperature via SNMP for a device.

    Args:
        db: Database session.
        device: The network device to poll.
        vendor: Vendor hint (mikrotik, huawei, generic). Auto-detected if None.

    Returns:
        {polled, stored, errors}
    """
    if not device.snmp_community:
        return {"polled": 0, "stored": 0, "errors": 0}

    ip = str(device.mgmt_ip or "")
    if not ip:
        return {"polled": 0, "stored": 0, "errors": 0}

    # Auto-detect vendor from sysDescr if not provided
    if not vendor:
        vendor_str = str(device.vendor or device.device_type or "").lower()
        if "mikrotik" in vendor_str or "routeros" in vendor_str:
            vendor = "mikrotik"
        elif "huawei" in vendor_str:
            vendor = "huawei"
        else:
            vendor = "generic"

    oid_map = _VENDOR_HEALTH_OIDS.get(vendor, _VENDOR_HEALTH_OIDS["generic"])

    polled = 0
    stored = 0
    errors = 0
    now = datetime.now(UTC)
    metric_type_map = {
        "cpu": MetricType.cpu,
        "memory_used": MetricType.memory,
        "temperature": MetricType.temperature,
        "uptime": MetricType.uptime,
    }

    for metric_name, oid in oid_map.items():
        try:
            value = _snmp_get_single(device, oid)
            if value is None:
                continue
            polled += 1

            # For memory, compute percentage if we have total
            numeric_value = float(value)

            # Map to MetricType
            mt = metric_type_map.get(metric_name)
            if mt is None:
                continue

            # Special: compute memory % from used/total
            if metric_name == "memory_used" and "memory_total" in oid_map:
                total_val = _snmp_get_single(device, oid_map["memory_total"])
                if total_val and float(total_val) > 0:
                    numeric_value = (float(value) / float(total_val)) * 100
                    polled += 1

            dm = DeviceMetric(
                device_id=device.id,
                metric_type=mt,
                value=numeric_value,
                recorded_at=now,
            )
            db.add(dm)
            stored += 1
        except Exception as exc:
            errors += 1
            logger.debug("SNMP poll failed for %s OID %s: %s", device.name, oid, exc)

    if stored > 0:
        db.flush()
        push_device_health_metrics(device, {
            k: v for k, v in [
                ("cpu", _latest_metric_value(db, device.id, MetricType.cpu)),
                ("memory", _latest_metric_value(db, device.id, MetricType.memory)),
                ("temperature", _latest_metric_value(db, device.id, MetricType.temperature)),
            ] if v is not None
        })

    return {"polled": polled, "stored": stored, "errors": errors}


def _latest_metric_value(db: Session, device_id, metric_type: MetricType) -> float | None:
    """Get the most recent metric value for a device."""
    row = db.scalars(
        select(DeviceMetric.value)
        .where(DeviceMetric.device_id == device_id, DeviceMetric.metric_type == metric_type)
        .order_by(DeviceMetric.recorded_at.desc())
        .limit(1)
    ).first()
    return float(row) if row is not None else None


def poll_onu_signal_strength(
    db: Session,
    olt_device: NetworkDevice,
) -> dict[str, int]:
    """Delegate ONT signal polling to the OLT polling service.

    Resolves the corresponding ``OLTDevice`` record for the monitoring device,
    updates ``OntUnit`` signal fields there, and returns summarized counts.
    """
    from app.models.network import OLTDevice
    from app.services.network import olt_polling as olt_polling_service

    host = str(olt_device.mgmt_ip or olt_device.hostname or "").strip()
    if not host:
        return {"polled": 0, "stored": 0, "low_signal": 0, "errors": 0}

    olt = db.scalars(
        select(OLTDevice).where(
            OLTDevice.is_active.is_(True),
            (OLTDevice.mgmt_ip == host) | (OLTDevice.hostname == host),
        )
    ).first()
    if not olt:
        logger.warning(
            "Monitoring device %s has no matching OLT inventory record for signal polling",
            olt_device.name,
        )
        return {"polled": 0, "stored": 0, "low_signal": 0, "errors": 1}

    community = None
    if olt.snmp_ro_community:
        try:
            from app.services.credential_crypto import decrypt_credential

            community = decrypt_credential(olt.snmp_ro_community)
        except Exception as exc:
            logger.warning("Failed to decrypt OLT SNMP community for %s: %s", olt.name, exc)
            return {"polled": 0, "stored": 0, "low_signal": 0, "errors": 1}

    result = olt_polling_service.poll_olt_ont_signals(db, olt, community=community)
    olt_polling_service.push_signal_metrics_to_victoriametrics(db)

    warning_threshold, _ = olt_polling_service.get_signal_thresholds(db)
    # Count low-signal ONTs directly from updated inventory.
    from app.models.network import OntUnit

    low_signal = db.scalar(
        select(func.count())
        .select_from(OntUnit)
        .where(
            OntUnit.is_active.is_(True),
            OntUnit.olt_device_id == olt.id,
            OntUnit.olt_rx_signal_dbm.is_not(None),
            OntUnit.olt_rx_signal_dbm < warning_threshold,
        )
    ) or 0

    return {
        "polled": int(result.get("polled", 0)),
        "stored": int(result.get("updated", 0)),
        "low_signal": int(low_signal),
        "errors": int(result.get("errors", 0)),
    }
