"""Query per-ONT time-series metrics from VictoriaMetrics.

Provides signal history and traffic history for ONT detail page charts.
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta

import httpx

logger = logging.getLogger(__name__)

_VM_URL = os.getenv("VICTORIAMETRICS_URL", "http://victoriametrics:8428")

# Step resolution per time range
_RANGE_STEPS: dict[str, str] = {
    "6h": "2m",
    "24h": "5m",
    "7d": "30m",
    "30d": "2h",
}


@dataclass
class ChartSeries:
    """A single time-series for chart rendering."""

    label: str
    timestamps: list[str] = field(default_factory=list)
    values: list[float | None] = field(default_factory=list)


@dataclass
class ChartData:
    """Complete chart data payload for the frontend."""

    series: list[ChartSeries] = field(default_factory=list)
    time_range: str = "24h"
    available: bool = False
    error: str | None = None


def _parse_range(time_range: str) -> tuple[datetime, datetime, str]:
    """Parse a time range string into (start, end, step)."""
    now = datetime.now(UTC)
    hours_map: dict[str, int] = {
        "6h": 6,
        "24h": 24,
        "7d": 168,
        "30d": 720,
    }
    hours = hours_map.get(time_range, 24)
    start = now - timedelta(hours=hours)
    step = _RANGE_STEPS.get(time_range, "5m")
    return start, now, step


def _query_range_sync(
    query: str, start: datetime, end: datetime, step: str
) -> list[dict]:
    """Execute a PromQL range query synchronously.

    Returns list of result dicts with 'metric' and 'values' keys.
    """
    try:
        with httpx.Client(timeout=15.0) as client:
            resp = client.get(
                f"{_VM_URL}/api/v1/query_range",
                params={
                    "query": query,
                    "start": start.isoformat(),
                    "end": end.isoformat(),
                    "step": step,
                },
            )
            resp.raise_for_status()
            data = resp.json()
            if data.get("status") != "success":
                return []
            return data.get("data", {}).get("result", [])
    except httpx.HTTPError as e:
        logger.error("VictoriaMetrics range query failed: %s", e)
        return []


def _result_to_series(result: dict, label: str) -> ChartSeries:
    """Convert a VictoriaMetrics result dict to a ChartSeries."""
    timestamps: list[str] = []
    values: list[float | None] = []
    for ts, val in result.get("values", []):
        dt = datetime.fromtimestamp(float(ts), tz=UTC)
        timestamps.append(dt.strftime("%Y-%m-%dT%H:%M:%SZ"))
        try:
            values.append(float(val))
        except (ValueError, TypeError):
            values.append(None)
    return ChartSeries(label=label, timestamps=timestamps, values=values)


def get_signal_history(ont_serial: str, time_range: str = "24h") -> ChartData:
    """Query signal level history for an ONT.

    Args:
        ont_serial: ONT serial number.
        time_range: Time range string (6h, 24h, 7d, 30d).

    Returns:
        ChartData with ONU Rx and OLT Rx series.
    """
    start, end, step = _parse_range(time_range)

    onu_query = f'ont_onu_rx_dbm{{ont_serial="{ont_serial}"}}'
    olt_query = f'ont_olt_rx_dbm{{ont_serial="{ont_serial}"}}'

    onu_results = _query_range_sync(onu_query, start, end, step)
    olt_results = _query_range_sync(olt_query, start, end, step)

    series: list[ChartSeries] = []
    if onu_results:
        series.append(_result_to_series(onu_results[0], "ONU Rx (dBm)"))
    if olt_results:
        series.append(_result_to_series(olt_results[0], "OLT Rx (dBm)"))

    if not series:
        return ChartData(
            time_range=time_range,
            available=False,
            error="No signal history data available for this ONT.",
        )

    return ChartData(series=series, time_range=time_range, available=True)


def get_traffic_history(ont_serial: str, time_range: str = "24h") -> ChartData:
    """Query traffic history for an ONT.

    Uses RADIUS accounting or SNMP-derived traffic counters if available.

    Args:
        ont_serial: ONT serial number.
        time_range: Time range string (6h, 24h, 7d, 30d).

    Returns:
        ChartData with Rx and Tx throughput series.
    """
    start, end, step = _parse_range(time_range)

    rx_query = f'rate(ont_rx_bytes_total{{ont_serial="{ont_serial}"}}[5m]) * 8'
    tx_query = f'rate(ont_tx_bytes_total{{ont_serial="{ont_serial}"}}[5m]) * 8'

    rx_results = _query_range_sync(rx_query, start, end, step)
    tx_results = _query_range_sync(tx_query, start, end, step)

    series: list[ChartSeries] = []
    if rx_results:
        series.append(_result_to_series(rx_results[0], "Download (bps)"))
    if tx_results:
        series.append(_result_to_series(tx_results[0], "Upload (bps)"))

    if not series:
        return ChartData(
            time_range=time_range,
            available=False,
            error="No traffic history data available for this ONT.",
        )

    return ChartData(series=series, time_range=time_range, available=True)
