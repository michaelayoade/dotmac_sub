"""Service helpers for ONT chart web routes."""

from __future__ import annotations

import logging
from datetime import UTC, datetime

from sqlalchemy.orm import Session

from app.models.network import OntUnit
from app.services.acs_client import create_acs_state_reader
from app.services.network.ont_metrics import (
    ChartData,
    ChartSeries,
    get_signal_history,
    get_traffic_history,
)
from app.services.network.signal_thresholds import get_signal_thresholds

logger = logging.getLogger(__name__)


def _build_signal_fallback_from_ont(ont: OntUnit, time_range: str) -> ChartData:
    """Build a one-point signal chart from current ONT snapshot fields."""
    timestamp = getattr(ont, "signal_updated_at", None)
    if timestamp is None:
        return ChartData(
            time_range=time_range,
            available=False,
            error="No signal history data available for this ONT.",
        )
    if timestamp.tzinfo is None:
        timestamp = timestamp.replace(tzinfo=UTC)
    ts = timestamp.strftime("%Y-%m-%dT%H:%M:%SZ")

    series = []
    if getattr(ont, "onu_rx_signal_dbm", None) is not None:
        series.append(
            ChartSeries(
                label="ONU Rx (dBm)",
                timestamps=[ts],
                values=[float(ont.onu_rx_signal_dbm)],
            )
        )
    if getattr(ont, "olt_rx_signal_dbm", None) is not None:
        series.append(
            ChartSeries(
                label="OLT Rx (dBm)",
                timestamps=[ts],
                values=[float(ont.olt_rx_signal_dbm)],
            )
        )

    if not series:
        return ChartData(
            time_range=time_range,
            available=False,
            error="No signal history data available for this ONT.",
        )

    return ChartData(
        series=series,
        time_range=time_range,
        available=True,
        error="Showing latest signal snapshot while historical series are unavailable.",
    )


def _parse_snapshot_time(snapshot: dict | None) -> datetime | None:
    if not isinstance(snapshot, dict):
        return None
    raw = snapshot.get("fetched_at")
    if isinstance(raw, str) and raw:
        try:
            parsed = datetime.fromisoformat(raw)
            return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)
        except ValueError:
            return None
    return None


def _counter_value(port: dict, *keys: str) -> int | None:
    for key in keys:
        value = port.get(key)
        if value in (None, ""):
            continue
        try:
            return int(str(value).strip())
        except (TypeError, ValueError):
            continue
    return None


def _sum_port_counters(ports: list[dict]) -> tuple[int | None, int | None]:
    sent_total = 0
    received_total = 0
    has_sent = False
    has_received = False
    for port in ports or []:
        sent = _counter_value(port, "bytes_sent", "Stats.BytesSent", "BytesSent")
        received = _counter_value(
            port,
            "bytes_received",
            "Stats.BytesReceived",
            "BytesReceived",
        )
        if sent is not None:
            sent_total += sent
            has_sent = True
        if received is not None:
            received_total += received
            has_received = True
    return (
        sent_total if has_sent else None,
        received_total if has_received else None,
    )


def _build_traffic_from_tr069_delta(
    db: Session,
    ont: OntUnit,
    time_range: str,
) -> ChartData:
    """Build a live ONT traffic point from TR-069 Ethernet byte counter deltas."""
    previous_snapshot = dict(getattr(ont, "tr069_last_snapshot", None) or {})
    previous_time = _parse_snapshot_time(previous_snapshot) or getattr(
        ont, "tr069_last_snapshot_at", None
    )
    previous_sent, previous_received = _sum_port_counters(
        list(previous_snapshot.get("ethernet_ports") or [])
    )

    summary = create_acs_state_reader().get_device_summary(
        db,
        str(ont.id),
        persist_observed_runtime=True,
    )
    if not summary.available:
        return ChartData(
            time_range=time_range,
            available=False,
            error=summary.error
            or "No live ONT traffic counters are available from TR-069.",
        )

    current_time = summary.fetched_at or datetime.now(UTC)
    if current_time.tzinfo is None:
        current_time = current_time.replace(tzinfo=UTC)
    if previous_time and previous_time.tzinfo is None:
        previous_time = previous_time.replace(tzinfo=UTC)

    current_sent, current_received = _sum_port_counters(summary.ethernet_ports)
    if (
        previous_time is None
        or previous_sent is None
        or previous_received is None
        or current_sent is None
        or current_received is None
    ):
        return ChartData(
            time_range=time_range,
            available=False,
            error=(
                "No live ONT traffic graph yet. Waiting for two OLT/ONT counter "
                "samples with BytesSent/BytesReceived."
            ),
        )

    elapsed = (current_time - previous_time).total_seconds()
    if elapsed <= 0:
        return ChartData(
            time_range=time_range,
            available=False,
            error="Waiting for the next ONT counter sample to calculate throughput.",
        )

    download_bps = max(0.0, (current_sent - previous_sent) * 8 / elapsed)
    upload_bps = max(0.0, (current_received - previous_received) * 8 / elapsed)
    current_ts = current_time.strftime("%Y-%m-%dT%H:%M:%SZ")
    previous_ts = previous_time.strftime("%Y-%m-%dT%H:%M:%SZ")
    return ChartData(
        series=[
            ChartSeries(
                label="Download (bps)",
                timestamps=[previous_ts, current_ts],
                values=[download_bps, download_bps],
            ),
            ChartSeries(
                label="Upload (bps)",
                timestamps=[previous_ts, current_ts],
                values=[upload_bps, upload_bps],
            ),
        ],
        time_range=time_range,
        available=True,
        error="Showing live ONT Ethernet counter delta from TR-069.",
    )


def charts_tab_data(
    db: Session,
    ont_id: str,
    time_range: str = "24h",
) -> dict[str, object]:
    """Build context for the Charts tab partial template.

    Args:
        db: Database session.
        ont_id: OntUnit ID.
        time_range: Time range string (6h, 24h, 7d, 30d).

    Returns:
        Template context dict with chart data.
    """
    ont = db.get(OntUnit, ont_id)
    if not ont:
        return {
            "signal_chart": ChartData(error="ONT not found."),
            "traffic_chart": ChartData(error="ONT not found."),
            "time_range": time_range,
            "ont_id": ont_id,
        }

    # Validate time range
    valid_ranges = {"6h", "24h", "7d", "30d"}
    if time_range not in valid_ranges:
        time_range = "24h"

    signal_chart = get_signal_history(
        ont.serial_number,
        time_range,
        ont_id=str(ont.id),
    )
    if not signal_chart.available or not signal_chart.series:
        signal_chart = _build_signal_fallback_from_ont(ont, time_range)
    traffic_chart = get_traffic_history(
        ont.serial_number,
        time_range,
        ont_id=str(ont.id),
    )
    if not traffic_chart.available or not traffic_chart.series:
        traffic_chart = _build_traffic_from_tr069_delta(db, ont, time_range)

    # Get thresholds for chart reference lines
    warn_thresh, crit_thresh = get_signal_thresholds(db)

    return {
        "signal_chart": signal_chart,
        "traffic_chart": traffic_chart,
        "time_range": time_range,
        "ont_id": ont_id,
        "warn_threshold": warn_thresh,
        "crit_threshold": crit_thresh,
    }
