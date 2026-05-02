"""Push ONT traffic and OLT health metrics to VictoriaMetrics."""

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
    """Push per-ONT traffic metrics to VictoriaMetrics.

    ONT online/offline and optical signal monitoring is read directly from
    Zabbix elsewhere. This helper only exports recent TR-069 traffic counters
    so dashboard status cannot drift through a secondary metrics system.

    Returns:
        Number of metric lines written.
    """
    # Collect ONTs with traffic data and their OLT/PON info.
    stmt = (
        select(
            OntUnit.id.label("ont_id"),
            OntUnit.serial_number,
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
            OntUnit.tr069_last_snapshot_at.is_not(None),
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
        logger.info("Pushed %d ONT traffic metric lines to VictoriaMetrics", len(lines))
    except httpx.HTTPError as e:
        logger.error("Failed to push signal metrics to VictoriaMetrics: %s", e)

    return len(lines)


def push_ont_traffic_metrics_to_victoriametrics(db: Session) -> int:
    """Push recent TR-069 ONT traffic metrics to VictoriaMetrics."""
    return _push_signal_metrics(db)
