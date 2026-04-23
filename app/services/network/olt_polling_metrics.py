"""Push ONT signal, traffic, and OLT health metrics to VictoriaMetrics."""

from __future__ import annotations

import logging
import os
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.models.network import (
    OLTDevice,
    OntAssignment,
    OntUnit,
    PonPort,
)
from app.services.network.olt_polling_parsers import OltHealthReading

logger = logging.getLogger(__name__)

_VM_URL = os.getenv("VICTORIAMETRICS_URL", "http://victoriametrics:8428")

# How recently a TR-069 snapshot must be to push traffic metrics (avoid stale data)
_TRAFFIC_SNAPSHOT_MAX_AGE_HOURS = 24


def _extract_traffic_bytes(snapshot: dict[str, Any] | None) -> tuple[int | None, int | None]:
    """Extract total bytes sent/received from TR-069 ethernet port snapshot.

    Args:
        snapshot: TR-069 last snapshot dict containing ethernet_ports.

    Returns:
        Tuple of (bytes_sent, bytes_received), either may be None if unavailable.
    """
    if not snapshot:
        return None, None

    ports = snapshot.get("ethernet_ports") or []
    if not ports:
        return None, None

    sent_total = 0
    received_total = 0
    has_sent = False
    has_received = False

    for port in ports:
        if not isinstance(port, dict):
            continue
        # Try multiple key formats (TR-069 variations)
        for sent_key in ("bytes_sent", "Stats.BytesSent", "BytesSent"):
            val = port.get(sent_key)
            if val not in (None, ""):
                try:
                    sent_total += int(str(val).strip())
                    has_sent = True
                    break
                except (TypeError, ValueError):
                    continue

        for recv_key in ("bytes_received", "Stats.BytesReceived", "BytesReceived"):
            val = port.get(recv_key)
            if val not in (None, ""):
                try:
                    received_total += int(str(val).strip())
                    has_received = True
                    break
                except (TypeError, ValueError):
                    continue

    return (sent_total if has_sent else None, received_total if has_received else None)


def _push_signal_metrics(db: Session) -> int:
    """Push per-ONT signal and traffic metrics to VictoriaMetrics.

    Reads current signal data and TR-069 traffic snapshots from the database
    and writes Prometheus line protocol to VictoriaMetrics' import endpoint.

    Returns:
        Number of metric lines written.
    """
    # Import here to avoid circular imports at module level
    from app.services.network.signal_thresholds import get_signal_thresholds

    # Collect ONTs with signal or traffic data and their OLT/PON info
    stmt = (
        select(
            OntUnit.id.label("ont_id"),
            OntUnit.serial_number,
            OntUnit.olt_rx_signal_dbm,
            OntUnit.onu_rx_signal_dbm,
            OntUnit.onu_tx_signal_dbm,
            OntUnit.ont_temperature_c,
            OntUnit.ont_voltage_v,
            OntUnit.ont_bias_current_ma,
            OntUnit.tr069_last_snapshot,
            OntUnit.tr069_last_snapshot_at,
            OLTDevice.name.label("olt_name"),
            PonPort.name.label("pon_port_name"),
        )
        .select_from(OntUnit)
        .outerjoin(
            OntAssignment,
            (OntAssignment.ont_unit_id == OntUnit.id)
            & (OntAssignment.active.is_(True)),
        )
        .outerjoin(PonPort, PonPort.id == OntAssignment.pon_port_id)
        .outerjoin(
            OLTDevice,
            OLTDevice.id == func.coalesce(PonPort.olt_id, OntUnit.olt_device_id),
        )
        .where(
            OntUnit.is_active.is_(True),
            # Include ONTs with signal data OR traffic snapshots
            (OntUnit.signal_updated_at.is_not(None))
            | (OntUnit.tr069_last_snapshot_at.is_not(None)),
        )
    )
    rows = db.execute(stmt).all()

    if not rows:
        return 0

    now = datetime.now(UTC)
    now_ms = int(now.timestamp() * 1000)
    traffic_cutoff = now - timedelta(hours=_TRAFFIC_SNAPSHOT_MAX_AGE_HOURS)
    lines: list[str] = []

    seen_serials: set[str] = set()
    for row in rows:
        serial = row.serial_number
        if not serial or serial in seen_serials:
            continue
        seen_serials.add(serial)
        olt_name = row.olt_name or "unknown"
        pon_port = row.pon_port_name or "unknown"
        ont_id = str(row.ont_id)
        labels = (
            f'ont_id="{ont_id}",ont_serial="{serial}",'
            f'olt_name="{olt_name}",pon_port="{pon_port}"'
        )

        # Signal metrics
        if row.olt_rx_signal_dbm is not None:
            lines.append(f"ont_olt_rx_dbm{{{labels}}} {row.olt_rx_signal_dbm} {now_ms}")
        if row.onu_rx_signal_dbm is not None:
            lines.append(f"ont_onu_rx_dbm{{{labels}}} {row.onu_rx_signal_dbm} {now_ms}")
        if row.onu_tx_signal_dbm is not None:
            lines.append(f"ont_onu_tx_dbm{{{labels}}} {row.onu_tx_signal_dbm} {now_ms}")
        if row.ont_temperature_c is not None:
            lines.append(
                f"ont_temperature_c{{{labels}}} {row.ont_temperature_c} {now_ms}"
            )
        if row.ont_voltage_v is not None:
            lines.append(f"ont_voltage_v{{{labels}}} {row.ont_voltage_v} {now_ms}")
        if row.ont_bias_current_ma is not None:
            lines.append(
                f"ont_bias_current_ma{{{labels}}} {row.ont_bias_current_ma} {now_ms}"
            )

        # Traffic metrics from TR-069 snapshot (if recent enough)
        snapshot_at = row.tr069_last_snapshot_at
        if snapshot_at is not None:
            # Normalize timezone
            if snapshot_at.tzinfo is None:
                snapshot_at = snapshot_at.replace(tzinfo=UTC)
            if snapshot_at >= traffic_cutoff:
                bytes_sent, bytes_received = _extract_traffic_bytes(
                    row.tr069_last_snapshot
                )
                # Use snapshot timestamp for traffic metrics (reflects when data was collected)
                snapshot_ms = int(snapshot_at.timestamp() * 1000)
                if bytes_sent is not None:
                    lines.append(
                        f"ont_tx_bytes_total{{{labels}}} {bytes_sent} {snapshot_ms}"
                    )
                if bytes_received is not None:
                    lines.append(
                        f"ont_rx_bytes_total{{{labels}}} {bytes_received} {snapshot_ms}"
                    )

    # Aggregate effective service status counts for dashboards.
    status_counts = db.execute(
        select(OntUnit.effective_status, func.count())
        .where(OntUnit.is_active.is_(True))
        .group_by(OntUnit.effective_status)
    ).all()

    for status_val, count in status_counts:
        status_str = (
            status_val.value if hasattr(status_val, "value") else str(status_val)
        )
        lines.append(f'onu_status_total{{status="{status_str}"}} {count} {now_ms}')

    # Expose raw OLT link counts separately so physical state remains visible.
    olt_status_counts = db.execute(
        select(OntUnit.online_status, func.count())
        .where(OntUnit.is_active.is_(True))
        .group_by(OntUnit.online_status)
    ).all()

    for status_val, count in olt_status_counts:
        status_str = (
            status_val.value if hasattr(status_val, "value") else str(status_val)
        )
        lines.append(f'onu_olt_status_total{{status="{status_str}"}} {count} {now_ms}')

    # Signal quality counts
    warn_thresh, crit_thresh = get_signal_thresholds(db)
    warning_count = (
        db.scalar(
            select(func.count())
            .select_from(OntUnit)
            .where(
                OntUnit.is_active.is_(True),
                OntUnit.olt_rx_signal_dbm.is_not(None),
                OntUnit.olt_rx_signal_dbm < warn_thresh,
                OntUnit.olt_rx_signal_dbm >= crit_thresh,
            )
        )
        or 0
    )
    critical_count = (
        db.scalar(
            select(func.count())
            .select_from(OntUnit)
            .where(
                OntUnit.is_active.is_(True),
                OntUnit.olt_rx_signal_dbm.is_not(None),
                OntUnit.olt_rx_signal_dbm < crit_thresh,
            )
        )
        or 0
    )
    lines.append(f'onu_signal_low{{severity="warning"}} {warning_count} {now_ms}')
    lines.append(f'onu_signal_low{{severity="critical"}} {critical_count} {now_ms}')

    if not lines:
        return 0

    # Write to VictoriaMetrics
    try:
        with httpx.Client(timeout=10.0) as client:
            resp = client.post(
                f"{_VM_URL}/api/v1/import/prometheus",
                content="\n".join(lines),
                headers={"Content-Type": "text/plain"},
            )
            resp.raise_for_status()
        logger.info("Pushed %d ONT signal metric lines to VictoriaMetrics", len(lines))
    except httpx.HTTPError as e:
        logger.error("Failed to push signal metrics to VictoriaMetrics: %s", e)

    return len(lines)


def push_signal_metrics_to_victoriametrics(db: Session) -> int:
    """Public wrapper for pushing current ONT signal metrics to VictoriaMetrics."""
    return _push_signal_metrics(db)


def _push_olt_health_metrics(health_map: dict[str, OltHealthReading]) -> int:
    """Push OLT health metrics to VictoriaMetrics.

    Args:
        health_map: Dict of OLT name -> OltHealthReading.

    Returns:
        Number of metric lines written.
    """
    if not health_map:
        return 0

    now_ms = int(datetime.now(UTC).timestamp() * 1000)
    lines: list[str] = []

    for olt_name, reading in health_map.items():
        labels = f'olt_name="{olt_name}"'
        if reading.cpu_percent is not None:
            lines.append(f"olt_cpu_percent{{{labels}}} {reading.cpu_percent} {now_ms}")
        if reading.temperature_c is not None:
            lines.append(
                f"olt_temperature_celsius{{{labels}}} {reading.temperature_c} {now_ms}"
            )
        if reading.memory_percent is not None:
            lines.append(
                f"olt_memory_percent{{{labels}}} {reading.memory_percent} {now_ms}"
            )
        if reading.uptime_seconds is not None:
            lines.append(
                f"olt_uptime_seconds{{{labels}}} {reading.uptime_seconds} {now_ms}"
            )

    if not lines:
        return 0

    try:
        with httpx.Client(timeout=10.0) as client:
            resp = client.post(
                f"{_VM_URL}/api/v1/import/prometheus",
                content="\n".join(lines),
                headers={"Content-Type": "text/plain"},
            )
            resp.raise_for_status()
        logger.info("Pushed %d OLT health metric lines to VictoriaMetrics", len(lines))
    except httpx.HTTPError as e:
        logger.error("Failed to push OLT health metrics to VictoriaMetrics: %s", e)

    return len(lines)
