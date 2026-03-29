"""Push ONT signal and OLT health metrics to VictoriaMetrics."""

from __future__ import annotations

import logging
import os
from datetime import UTC, datetime

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


def _push_signal_metrics(db: Session) -> int:
    """Push per-ONT signal metrics and aggregate status counts to VictoriaMetrics.

    Reads current signal data from the database and writes Prometheus line
    protocol to VictoriaMetrics' import endpoint (sync HTTP).

    Returns:
        Number of metric lines written.
    """
    # Import here to avoid circular imports at module level
    from app.services.network.olt_polling import get_signal_thresholds

    # Collect ONTs with recent signal data and their OLT/PON info
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
            OntUnit.online_status,
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
            OntUnit.signal_updated_at.is_not(None),
        )
    )
    rows = db.execute(stmt).all()

    if not rows:
        return 0

    now_ms = int(datetime.now(UTC).timestamp() * 1000)
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

        if row.olt_rx_signal_dbm is not None:
            lines.append(f"ont_olt_rx_dbm{{{labels}}} {row.olt_rx_signal_dbm} {now_ms}")
        if row.onu_rx_signal_dbm is not None:
            lines.append(f"ont_onu_rx_dbm{{{labels}}} {row.onu_rx_signal_dbm} {now_ms}")
        if row.onu_tx_signal_dbm is not None:
            lines.append(f"ont_onu_tx_dbm{{{labels}}} {row.onu_tx_signal_dbm} {now_ms}")
        if row.ont_temperature_c is not None:
            lines.append(f"ont_temperature_c{{{labels}}} {row.ont_temperature_c} {now_ms}")
        if row.ont_voltage_v is not None:
            lines.append(f"ont_voltage_v{{{labels}}} {row.ont_voltage_v} {now_ms}")
        if row.ont_bias_current_ma is not None:
            lines.append(f"ont_bias_current_ma{{{labels}}} {row.ont_bias_current_ma} {now_ms}")

    # Aggregate status counts
    status_counts = db.execute(
        select(OntUnit.online_status, func.count())
        .where(OntUnit.is_active.is_(True))
        .group_by(OntUnit.online_status)
    ).all()

    for status_val, count in status_counts:
        status_str = (
            status_val.value if hasattr(status_val, "value") else str(status_val)
        )
        lines.append(f'onu_status_total{{status="{status_str}"}} {count} {now_ms}')

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
