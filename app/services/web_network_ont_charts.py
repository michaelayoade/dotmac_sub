"""Service helpers for ONT chart web routes."""

from __future__ import annotations

from datetime import UTC, datetime

from sqlalchemy.orm import Session

from app.models.network import OntUnit
from app.services.network.ont_metrics import (
    ChartData,
    ChartSeries,
    get_signal_history,
    get_traffic_history,
)
from app.services.network.signal_thresholds import get_signal_thresholds


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
    return (sent_total if has_sent else None, received_total if has_received else None)


def _build_traffic_from_snapshot(ont: OntUnit, time_range: str) -> ChartData:
    """Build a one-point traffic chart from the last persisted TR-069 snapshot."""
    snapshot = dict(getattr(ont, "tr069_last_snapshot", None) or {})
    timestamp = getattr(ont, "tr069_last_snapshot_at", None)
    if timestamp is None:
        return ChartData(
            time_range=time_range,
            available=False,
            error=(
                "No traffic history data available yet. Waiting for metrics sync or "
                "a persisted TR-069 snapshot."
            ),
        )

    if timestamp.tzinfo is None:
        timestamp = timestamp.replace(tzinfo=UTC)

    bytes_sent, bytes_received = _sum_port_counters(
        list(snapshot.get("ethernet_ports") or [])
    )
    if bytes_sent is None and bytes_received is None:
        return ChartData(
            time_range=time_range,
            available=False,
            error=(
                "No traffic history data available yet. The last persisted TR-069 "
                "snapshot does not include Ethernet byte counters."
            ),
        )

    current_ts = timestamp.strftime("%Y-%m-%dT%H:%M:%SZ")
    datasets: list[ChartSeries] = []
    if bytes_sent is not None:
        datasets.append(
            ChartSeries(
                label="Bytes Sent",
                timestamps=[current_ts],
                values=[float(bytes_sent)],
            )
        )
    if bytes_received is not None:
        datasets.append(
            ChartSeries(
                label="Bytes Received",
                timestamps=[current_ts],
                values=[float(bytes_received)],
            )
        )

    return ChartData(
        series=datasets,
        time_range=time_range,
        available=True,
        error="Showing the last persisted TR-069 Ethernet counter snapshot.",
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
        traffic_chart = _build_traffic_from_snapshot(ont, time_range)

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
