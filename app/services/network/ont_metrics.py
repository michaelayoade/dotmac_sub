"""Query per-ONT time-series metrics from configured data source.

Provides signal history and traffic history for ONT detail page charts.
Uses the metrics adapter pattern to support multiple data sources
(VictoriaMetrics, Zabbix, or composite).

Configure via METRICS_ADAPTER env var:
- "victoriametrics" (default): Query VictoriaMetrics directly
- "zabbix": Query Zabbix API history
- "composite": Try VictoriaMetrics first, fall back to Zabbix
"""

from __future__ import annotations

from app.services.network.metrics_adapters import (
    ChartData,
    ChartSeries,
    get_metrics_adapter,
)

# Re-export dataclasses for backwards compatibility
__all__ = ["ChartData", "ChartSeries", "get_signal_history", "get_traffic_history"]


def get_signal_history(
    ont_serial: str, time_range: str = "24h", *, ont_id: str | None = None
) -> ChartData:
    """Query signal level history for an ONT.

    Uses the configured metrics adapter (VictoriaMetrics, Zabbix, or composite).

    Args:
        ont_serial: ONT serial number.
        time_range: Time range string (6h, 24h, 7d, 30d).
        ont_id: Optional ONT database ID for additional filtering.

    Returns:
        ChartData with ONU Rx and OLT Rx series.
    """
    adapter = get_metrics_adapter()
    return adapter.get_signal_history(ont_serial, time_range, ont_id=ont_id)


def get_traffic_history(
    ont_serial: str, time_range: str = "24h", *, ont_id: str | None = None
) -> ChartData:
    """Query traffic history for an ONT.

    Uses the configured metrics adapter (VictoriaMetrics, Zabbix, or composite).

    Args:
        ont_serial: ONT serial number.
        time_range: Time range string (6h, 24h, 7d, 30d).
        ont_id: Optional ONT database ID for additional filtering.

    Returns:
        ChartData with Rx and Tx throughput series.
    """
    adapter = get_metrics_adapter()
    return adapter.get_traffic_history(ont_serial, time_range, ont_id=ont_id)
