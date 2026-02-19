"""
VictoriaMetrics client for bandwidth metrics storage.

Provides methods for writing bandwidth samples and querying time series data
using VictoriaMetrics' Prometheus-compatible API.
"""
import logging
import os
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, cast

import httpx

from app.models.domain_settings import SettingDomain
from app.services.settings_spec import resolve_value

logger = logging.getLogger(__name__)

VICTORIAMETRICS_URL = os.getenv("VICTORIAMETRICS_URL", "http://victoriametrics:8428")
_DEFAULT_TIMEOUT = 30.0  # fallback when settings unavailable


@dataclass
class BandwidthPoint:
    """A single bandwidth measurement point."""
    timestamp: datetime
    subscription_id: str
    nas_device_id: str | None
    rx_bps: int
    tx_bps: int


@dataclass
class TimeSeriesPoint:
    """A single point in a time series query result."""
    timestamp: datetime
    value: float


@dataclass
class TimeSeriesResult:
    """Result from a time series query."""
    metric: dict[str, str]
    values: list[TimeSeriesPoint]


class MetricsStoreError(Exception):
    """Base exception for metrics store errors."""
    pass


class MetricsStore:
    """
    VictoriaMetrics client for bandwidth metrics.

    Uses Prometheus remote write format for ingestion and PromQL for queries.
    """

    def __init__(self, base_url: str | None = None, timeout: float | None = None):
        self.base_url = base_url or VICTORIAMETRICS_URL
        self._timeout = timeout
        self._client: httpx.AsyncClient | None = None

    def _get_timeout(self) -> float:
        """Get the HTTP timeout, using configurable settings."""
        if self._timeout is not None:
            return self._timeout
        # Try to get from settings (no db available in async context, use env fallback)
        timeout_obj = resolve_value(
            None, SettingDomain.bandwidth, "victoriametrics_timeout_seconds"
        )
        try:
            return float(str(timeout_obj)) if timeout_obj is not None else _DEFAULT_TIMEOUT
        except (TypeError, ValueError):
            return _DEFAULT_TIMEOUT

    async def _get_client(self) -> httpx.AsyncClient:
        if self._client is None or self._client.is_closed:
            self._client = httpx.AsyncClient(timeout=self._get_timeout())
        return self._client

    async def close(self):
        """Close the HTTP client."""
        if self._client and not self._client.is_closed:
            await self._client.aclose()

    def _to_prometheus_line(self, point: BandwidthPoint) -> str:
        """
        Convert a bandwidth point to Prometheus line protocol format.
        """
        labels = f'subscription_id="{point.subscription_id}"'
        if point.nas_device_id:
            labels += f',nas_device_id="{point.nas_device_id}"'

        timestamp_ms = int(point.timestamp.timestamp() * 1000)

        lines = [
            f"bandwidth_rx_bps{{{labels}}} {point.rx_bps} {timestamp_ms}",
            f"bandwidth_tx_bps{{{labels}}} {point.tx_bps} {timestamp_ms}",
        ]
        return "\n".join(lines)

    async def write_samples(self, points: list[BandwidthPoint]) -> bool:
        """
        Write bandwidth samples to VictoriaMetrics.

        Args:
            points: List of bandwidth measurement points

        Returns:
            True if write was successful
        """
        if not points:
            return True

        client = await self._get_client()
        lines = "\n".join(self._to_prometheus_line(p) for p in points)

        try:
            response = await client.post(
                f"{self.base_url}/api/v1/import/prometheus",
                content=lines,
                headers={"Content-Type": "text/plain"},
            )
            response.raise_for_status()
            return True
        except httpx.HTTPError as e:
            logger.error(f"Failed to write samples to VictoriaMetrics: {e}")
            raise MetricsStoreError(f"Write failed: {e}") from e

    async def write_aggregates(
        self,
        subscription_id: str,
        nas_device_id: str | None,
        timestamp: datetime,
        rx_avg: float,
        tx_avg: float,
        rx_max: float,
        tx_max: float,
        sample_count: int,
    ) -> bool:
        """
        Write pre-aggregated bandwidth data to VictoriaMetrics.

        This is used for pushing aggregates from the Celery worker.
        """
        client = await self._get_client()

        labels = f'subscription_id="{subscription_id}"'
        if nas_device_id:
            labels += f',nas_device_id="{nas_device_id}"'

        timestamp_ms = int(timestamp.timestamp() * 1000)

        lines = [
            f"bandwidth_rx_bps_avg{{{labels}}} {rx_avg} {timestamp_ms}",
            f"bandwidth_tx_bps_avg{{{labels}}} {tx_avg} {timestamp_ms}",
            f"bandwidth_rx_bps_max{{{labels}}} {rx_max} {timestamp_ms}",
            f"bandwidth_tx_bps_max{{{labels}}} {tx_max} {timestamp_ms}",
            f"bandwidth_sample_count{{{labels}}} {sample_count} {timestamp_ms}",
        ]

        try:
            response = await client.post(
                f"{self.base_url}/api/v1/import/prometheus",
                content="\n".join(lines),
                headers={"Content-Type": "text/plain"},
            )
            response.raise_for_status()
            return True
        except httpx.HTTPError as e:
            logger.error(f"Failed to write aggregates to VictoriaMetrics: {e}")
            raise MetricsStoreError(f"Write failed: {e}") from e

    async def query_range(
        self,
        query: str,
        start: datetime,
        end: datetime,
        step: str = "1m",
    ) -> list[TimeSeriesResult]:
        """
        Execute a PromQL range query.

        Args:
            query: PromQL query string
            start: Start time
            end: End time
            step: Query resolution (e.g., "1m", "5m", "1h")

        Returns:
            List of time series results
        """
        client = await self._get_client()

        params = {
            "query": query,
            "start": start.isoformat(),
            "end": end.isoformat(),
            "step": step,
        }

        try:
            response = await client.get(
                f"{self.base_url}/api/v1/query_range",
                params=params,
            )
            response.raise_for_status()
            data = response.json()

            if data.get("status") != "success":
                raise MetricsStoreError(f"Query failed: {data.get('error', 'Unknown error')}")

            results = []
            for result in data.get("data", {}).get("result", []):
                values = [
                    TimeSeriesPoint(
                        timestamp=datetime.fromtimestamp(ts, tz=UTC),
                        value=float(val),
                    )
                    for ts, val in result.get("values", [])
                ]
                results.append(TimeSeriesResult(
                    metric=result.get("metric", {}),
                    values=values,
                ))

            return results

        except httpx.HTTPError as e:
            logger.error(f"Failed to query VictoriaMetrics: {e}")
            raise MetricsStoreError(f"Query failed: {e}") from e

    async def get_instant(self, query: str) -> list[dict[str, Any]]:
        """
        Execute an instant PromQL query (current value).

        Args:
            query: PromQL query string

        Returns:
            List of metric results with current values
        """
        client = await self._get_client()

        try:
            response = await client.get(
                f"{self.base_url}/api/v1/query",
                params={"query": query},
            )
            response.raise_for_status()
            data = response.json()

            if not isinstance(data, dict):
                raise MetricsStoreError("Query failed: invalid response payload")
            if data.get("status") != "success":
                raise MetricsStoreError(f"Query failed: {data.get('error', 'Unknown error')}")

            data_obj = data.get("data")
            if not isinstance(data_obj, dict):
                return []
            result_obj = data_obj.get("result")
            if not isinstance(result_obj, list):
                return []
            return cast(list[dict[str, Any]], result_obj)

        except httpx.HTTPError as e:
            logger.error(f"Failed to query VictoriaMetrics: {e}")
            raise MetricsStoreError(f"Query failed: {e}") from e

    async def get_subscription_bandwidth(
        self,
        subscription_id: str,
        start: datetime,
        end: datetime,
        step: str = "1m",
    ) -> dict[str, list[TimeSeriesPoint]]:
        """
        Get bandwidth time series for a specific subscription.

        Args:
            subscription_id: The subscription UUID
            start: Start time
            end: End time
            step: Query resolution

        Returns:
            Dict with 'rx' and 'tx' keys containing time series points
        """
        rx_query = f'bandwidth_rx_bps{{subscription_id="{subscription_id}"}}'
        tx_query = f'bandwidth_tx_bps{{subscription_id="{subscription_id}"}}'

        rx_results = await self.query_range(rx_query, start, end, step)
        tx_results = await self.query_range(tx_query, start, end, step)

        return {
            "rx": rx_results[0].values if rx_results else [],
            "tx": tx_results[0].values if tx_results else [],
        }

    async def get_current_bandwidth(
        self,
        subscription_id: str,
    ) -> dict[str, float]:
        """
        Get current bandwidth for a subscription.

        Args:
            subscription_id: The subscription UUID

        Returns:
            Dict with rx_bps and tx_bps values
        """
        rx_query = f'bandwidth_rx_bps{{subscription_id="{subscription_id}"}}'
        tx_query = f'bandwidth_tx_bps{{subscription_id="{subscription_id}"}}'

        rx_results = await self.get_instant(rx_query)
        tx_results = await self.get_instant(tx_query)

        rx_bps = 0.0
        tx_bps = 0.0

        if rx_results and rx_results[0].get("value"):
            rx_bps = float(rx_results[0]["value"][1])
        if tx_results and tx_results[0].get("value"):
            tx_bps = float(tx_results[0]["value"][1])

        return {"rx_bps": rx_bps, "tx_bps": tx_bps}

    async def get_peak_bandwidth(
        self,
        subscription_id: str,
        start: datetime,
        end: datetime,
    ) -> dict[str, float]:
        """
        Get peak bandwidth for a subscription within a time range.
        """
        duration = int((end - start).total_seconds())
        rx_query = f'max_over_time(bandwidth_rx_bps{{subscription_id="{subscription_id}"}}[{duration}s])'
        tx_query = f'max_over_time(bandwidth_tx_bps{{subscription_id="{subscription_id}"}}[{duration}s])'

        rx_results = await self.get_instant(rx_query)
        tx_results = await self.get_instant(tx_query)

        rx_peak = 0.0
        tx_peak = 0.0

        if rx_results and rx_results[0].get("value"):
            rx_peak = float(rx_results[0]["value"][1])
        if tx_results and tx_results[0].get("value"):
            tx_peak = float(tx_results[0]["value"][1])

        return {"rx_peak_bps": rx_peak, "tx_peak_bps": tx_peak}

    async def get_total_bytes(
        self,
        subscription_id: str,
        start: datetime,
        end: datetime,
    ) -> dict[str, float]:
        """
        Get total bytes transferred for a subscription within a time range.
        """
        duration = int((end - start).total_seconds())
        # Convert bps to bytes by integrating over time (bps * seconds / 8)
        rx_query = f'sum(increase(bandwidth_rx_bps{{subscription_id="{subscription_id}"}}[{duration}s])) / 8'
        tx_query = f'sum(increase(bandwidth_tx_bps{{subscription_id="{subscription_id}"}}[{duration}s])) / 8'

        rx_results = await self.get_instant(rx_query)
        tx_results = await self.get_instant(tx_query)

        rx_bytes = 0.0
        tx_bytes = 0.0

        if rx_results and rx_results[0].get("value"):
            rx_bytes = float(rx_results[0]["value"][1])
        if tx_results and tx_results[0].get("value"):
            tx_bytes = float(tx_results[0]["value"][1])

        return {"rx_bytes": rx_bytes, "tx_bytes": tx_bytes}

    async def get_top_users(
        self,
        limit: int = 10,
        duration: str = "1h",
    ) -> list[dict[str, Any]]:
        """
        Get top bandwidth consumers.

        Args:
            limit: Number of top users to return
            duration: Time window for calculation

        Returns:
            List of top users with subscription_id and bandwidth
        """
        query = f'topk({limit}, sum by (subscription_id) (rate(bandwidth_rx_bps[{duration}]) + rate(bandwidth_tx_bps[{duration}])))'

        results = await self.get_instant(query)

        return [
            {
                "subscription_id": r.get("metric", {}).get("subscription_id"),
                "total_bps": float(r.get("value", [0, 0])[1]),
            }
            for r in results
        ]

    async def health_check(self) -> bool:
        """Check if VictoriaMetrics is healthy."""
        try:
            client = await self._get_client()
            response = await client.get(f"{self.base_url}/health")
            return response.status_code == 200
        except Exception:
            return False


# Singleton instance
_metrics_store: MetricsStore | None = None


def get_metrics_store() -> MetricsStore:
    """Get or create the metrics store singleton."""
    global _metrics_store
    if _metrics_store is None:
        _metrics_store = MetricsStore()
    return _metrics_store
