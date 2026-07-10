"""Metrics adapter protocol and implementations.

Provides a clean abstraction for fetching ONT signal and traffic metrics.
VictoriaMetrics is the metrics store (the Zabbix adapter was retired with the
native monitoring cutover).

Usage:
    from app.services.network.metrics_adapters import get_metrics_adapter

    adapter = get_metrics_adapter()
    chart_data = adapter.get_signal_history(ont_serial="ABC123", time_range="24h")
"""

from __future__ import annotations

import logging
import os
import re
from abc import ABC
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Protocol, runtime_checkable

import httpx

logger = logging.getLogger(__name__)


# ============================================================================
# Data Classes
# ============================================================================


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


# ============================================================================
# Protocol Definition
# ============================================================================


@runtime_checkable
class MetricsReader(Protocol):
    """Protocol for metrics readers.

    Implementations must provide methods to fetch signal and traffic
    history for ONT devices.
    """

    def get_signal_history(
        self,
        ont_serial: str,
        time_range: str = "24h",
        *,
        ont_id: str | None = None,
    ) -> ChartData:
        """Get signal level history for an ONT.

        Args:
            ont_serial: ONT serial number
            time_range: Time range string (6h, 24h, 7d, 30d)
            ont_id: Optional ONT database ID for additional filtering

        Returns:
            ChartData with ONU Rx and OLT Rx series
        """
        ...

    def get_traffic_history(
        self,
        ont_serial: str,
        time_range: str = "24h",
        *,
        ont_id: str | None = None,
    ) -> ChartData:
        """Get traffic history for an ONT.

        Args:
            ont_serial: ONT serial number
            time_range: Time range string (6h, 24h, 7d, 30d)
            ont_id: Optional ONT database ID for additional filtering

        Returns:
            ChartData with Rx and Tx throughput series
        """
        ...


# ============================================================================
# Shared Utilities
# ============================================================================


RANGE_STEPS: dict[str, str] = {
    "6h": "2m",
    "24h": "5m",
    "7d": "30m",
    "30d": "2h",
}


def parse_time_range(time_range: str) -> tuple[datetime, datetime, str]:
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
    step = RANGE_STEPS.get(time_range, "5m")
    return start, now, step


def normalize_serial(ont_serial: str) -> list[str]:
    """Generate serial number candidates for matching."""
    from app.services.genieacs_client import normalize_tr069_serial

    raw = str(ont_serial or "").strip()
    if not raw:
        return []
    candidates = {raw, raw.upper(), raw.lower()}
    normalized = normalize_tr069_serial(raw)
    if normalized:
        candidates.add(normalized)
    compact = re.sub(r"[^0-9A-Za-z]", "", raw)
    if compact:
        candidates.add(compact)
        candidates.add(compact.upper())
    return [c for c in candidates if c]


# ============================================================================
# VictoriaMetrics Adapter
# ============================================================================


class VictoriaMetricsAdapter(ABC, MetricsReader):
    """Adapter for reading metrics from VictoriaMetrics."""

    def __init__(self, base_url: str | None = None, timeout: float = 15.0):
        self.base_url = base_url or os.getenv(
            "VICTORIAMETRICS_URL", "http://victoriametrics:8428"
        )
        self.timeout = timeout

    def _query_range(
        self, query: str, start: datetime, end: datetime, step: str
    ) -> list[dict]:
        """Execute a PromQL range query."""
        try:
            with httpx.Client(timeout=self.timeout) as client:
                resp = client.get(
                    f"{self.base_url}/api/v1/query_range",
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
            logger.error("VictoriaMetrics query failed: %s", e)
            return []

    def _result_to_series(self, result: dict, label: str) -> ChartSeries:
        """Convert a VictoriaMetrics result to a ChartSeries."""
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

    def _build_label_selector(self, ont_serial: str, ont_id: str | None = None) -> str:
        """Build a PromQL label selector."""
        selectors: list[str] = []
        if ont_id:
            selectors.append(f'ont_id="{ont_id}"')
        serial_candidates = normalize_serial(ont_serial)
        if serial_candidates:
            if len(serial_candidates) == 1:
                selectors.append(f'ont_serial="{serial_candidates[0]}"')
            else:
                escaped = "|".join(re.escape(c) for c in sorted(serial_candidates))
                selectors.append(f'ont_serial=~"{escaped}"')
        return ",".join(selectors)

    def get_signal_history(
        self,
        ont_serial: str,
        time_range: str = "24h",
        *,
        ont_id: str | None = None,
    ) -> ChartData:
        """Get signal history from VictoriaMetrics."""
        start, end, step = parse_time_range(time_range)
        label_selector = self._build_label_selector(ont_serial, ont_id)

        if not label_selector:
            return ChartData(
                time_range=time_range,
                available=False,
                error="No signal history data available for this ONT.",
            )

        onu_query = f"ont_onu_rx_dbm{{{label_selector}}}"
        olt_query = f"ont_olt_rx_dbm{{{label_selector}}}"

        onu_results = self._query_range(onu_query, start, end, step)
        olt_results = self._query_range(olt_query, start, end, step)

        series: list[ChartSeries] = []
        if onu_results:
            series.append(self._result_to_series(onu_results[0], "ONU Rx (dBm)"))
        if olt_results:
            series.append(self._result_to_series(olt_results[0], "OLT Rx (dBm)"))

        if not series:
            return ChartData(
                time_range=time_range,
                available=False,
                error="No signal history data available for this ONT.",
            )

        return ChartData(series=series, time_range=time_range, available=True)

    def get_traffic_history(
        self,
        ont_serial: str,
        time_range: str = "24h",
        *,
        ont_id: str | None = None,
    ) -> ChartData:
        """Get traffic history from VictoriaMetrics."""
        start, end, step = parse_time_range(time_range)
        label_selector = self._build_label_selector(ont_serial, ont_id)

        if not label_selector:
            return ChartData(
                time_range=time_range,
                available=False,
                error="No traffic history data available for this ONT.",
            )

        counter_pairs = [
            ("ont_rx_bytes_total", "ont_tx_bytes_total"),
            ("onu_rx_bytes_total", "onu_tx_bytes_total"),
            ("ont_downstream_bytes_total", "ont_upstream_bytes_total"),
        ]

        for rx_metric, tx_metric in counter_pairs:
            rx_query = f"rate({rx_metric}{{{label_selector}}}[5m]) * 8"
            tx_query = f"rate({tx_metric}{{{label_selector}}}[5m]) * 8"

            rx_results = self._query_range(rx_query, start, end, step)
            tx_results = self._query_range(tx_query, start, end, step)

            series: list[ChartSeries] = []
            if rx_results:
                series.append(self._result_to_series(rx_results[0], "Download (bps)"))
            if tx_results:
                series.append(self._result_to_series(tx_results[0], "Upload (bps)"))
            if series:
                return ChartData(series=series, time_range=time_range, available=True)

        return ChartData(
            time_range=time_range,
            available=False,
            error="No traffic history data available for this ONT.",
        )


# ============================================================================
# Factory
# ============================================================================


_adapter_instance: MetricsReader | None = None


def get_metrics_adapter() -> MetricsReader:
    """Get the configured metrics adapter (VictoriaMetrics).

    Returns:
        Configured MetricsReader instance
    """
    global _adapter_instance

    if _adapter_instance is not None:
        return _adapter_instance

    _adapter_instance = VictoriaMetricsAdapter()

    logger.info("Initialized metrics adapter: %s", type(_adapter_instance).__name__)
    return _adapter_instance


def reset_metrics_adapter() -> None:
    """Reset the metrics adapter singleton (for testing)."""
    global _adapter_instance
    _adapter_instance = None
