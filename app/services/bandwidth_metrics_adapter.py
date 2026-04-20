"""Bandwidth metrics adapter for VictoriaMetrics.

Provides sync VictoriaMetrics writes for Celery tasks with batching and
subscription ID caching to reduce overhead and FK validation queries.
"""

from __future__ import annotations

import logging
import os
import time
from dataclasses import dataclass, field
from datetime import datetime
from typing import TYPE_CHECKING
from uuid import UUID

import httpx
from sqlalchemy import select

from app.services.adapters import adapter_registry

if TYPE_CHECKING:
    from sqlalchemy.orm import Session

logger = logging.getLogger(__name__)

VICTORIAMETRICS_URL = os.getenv("VICTORIAMETRICS_URL", "http://victoriametrics:8428")
_DEFAULT_TIMEOUT = 30.0
_DEFAULT_CACHE_TTL_SECONDS = 300  # 5 minutes


@dataclass
class BandwidthAggregate:
    """Pre-aggregated bandwidth data for a subscription."""

    subscription_id: str
    nas_device_id: str | None
    timestamp: datetime
    rx_avg: float
    tx_avg: float
    rx_max: float
    tx_max: float
    sample_count: int


@dataclass
class WriteResult:
    """Result of a VictoriaMetrics write operation."""

    success: bool
    written: int
    error: str | None = None


@dataclass
class _CacheEntry:
    """Internal cache entry with TTL tracking."""

    valid_ids: set[UUID]
    cached_at: float = field(default_factory=time.monotonic)


class VictoriaMetricsWriter:
    """Sync HTTP client for VictoriaMetrics writes.

    Unlike the async MetricsStore, this class uses synchronous httpx
    to avoid asyncio.run() overhead in Celery tasks.
    """

    def __init__(
        self,
        base_url: str | None = None,
        timeout: float | None = None,
    ):
        self.base_url = base_url or VICTORIAMETRICS_URL
        self._timeout = timeout or _DEFAULT_TIMEOUT
        self._client: httpx.Client | None = None

    def _get_client(self) -> httpx.Client:
        if self._client is None or self._client.is_closed:
            self._client = httpx.Client(timeout=self._timeout)
        return self._client

    def close(self) -> None:
        """Close the HTTP client."""
        if self._client and not self._client.is_closed:
            self._client.close()

    def _format_aggregate_lines(self, agg: BandwidthAggregate) -> list[str]:
        """Format a single aggregate as Prometheus line protocol lines."""
        labels = f'subscription_id="{agg.subscription_id}"'
        if agg.nas_device_id:
            labels += f',nas_device_id="{agg.nas_device_id}"'

        timestamp_ms = int(agg.timestamp.timestamp() * 1000)

        return [
            f"bandwidth_rx_bps_avg{{{labels}}} {agg.rx_avg} {timestamp_ms}",
            f"bandwidth_tx_bps_avg{{{labels}}} {agg.tx_avg} {timestamp_ms}",
            f"bandwidth_rx_bps_max{{{labels}}} {agg.rx_max} {timestamp_ms}",
            f"bandwidth_tx_bps_max{{{labels}}} {agg.tx_max} {timestamp_ms}",
            f"bandwidth_sample_count{{{labels}}} {agg.sample_count} {timestamp_ms}",
        ]

    def write_aggregates_batch(
        self,
        aggregates: list[BandwidthAggregate],
    ) -> WriteResult:
        """Write multiple aggregates in a single HTTP request.

        Args:
            aggregates: List of bandwidth aggregates to write.

        Returns:
            WriteResult with success status and count of written aggregates.
        """
        if not aggregates:
            return WriteResult(success=True, written=0)

        # Build all lines for the batch
        all_lines: list[str] = []
        for agg in aggregates:
            all_lines.extend(self._format_aggregate_lines(agg))

        content = "\n".join(all_lines)

        try:
            client = self._get_client()
            response = client.post(
                f"{self.base_url}/api/v1/import/prometheus",
                content=content,
                headers={"Content-Type": "text/plain"},
            )
            response.raise_for_status()
            logger.debug(
                "Wrote %d aggregates (%d lines) to VictoriaMetrics",
                len(aggregates),
                len(all_lines),
            )
            return WriteResult(success=True, written=len(aggregates))
        except httpx.HTTPError as e:
            error_msg = str(e)
            logger.error("Failed to write aggregates to VictoriaMetrics: %s", error_msg)
            return WriteResult(success=False, written=0, error=error_msg)

    def health_check(self) -> bool:
        """Check if VictoriaMetrics is healthy."""
        try:
            client = self._get_client()
            response = client.get(f"{self.base_url}/health")
            return response.status_code == 200
        except Exception:
            return False


class SubscriptionIdCache:
    """TTL-based cache for valid subscription IDs.

    Reduces database queries for FK validation by caching known-valid
    subscription IDs for a configurable TTL (default 5 minutes).
    """

    def __init__(self, ttl_seconds: float = _DEFAULT_CACHE_TTL_SECONDS):
        self._ttl_seconds = ttl_seconds
        self._cache: _CacheEntry | None = None

    def _is_cache_valid(self) -> bool:
        if self._cache is None:
            return False
        elapsed = time.monotonic() - self._cache.cached_at
        return elapsed < self._ttl_seconds

    def filter_valid(self, db: Session, subscription_ids: set[UUID]) -> set[UUID]:
        """Filter subscription IDs to only those that exist in the database.

        Uses caching to reduce database queries. IDs not in cache are
        validated against the database and the cache is refreshed.

        Args:
            db: Database session.
            subscription_ids: Set of subscription IDs to validate.

        Returns:
            Set of valid (existing) subscription IDs.
        """
        if not subscription_ids:
            return set()

        # Check cache first
        if self._is_cache_valid() and self._cache is not None:
            # Return intersection of requested IDs and cached valid IDs
            cached_valid = subscription_ids & self._cache.valid_ids
            uncached = subscription_ids - self._cache.valid_ids

            # If all requested IDs are in cache, return immediately
            if not uncached:
                return cached_valid

            # Query only uncached IDs
            from app.models.catalog import Subscription

            newly_valid = set(
                db.scalars(
                    select(Subscription.id).where(Subscription.id.in_(uncached))
                ).all()
            )

            # Update cache with new IDs
            self._cache.valid_ids.update(newly_valid)
            self._cache.cached_at = time.monotonic()

            return cached_valid | newly_valid

        # No valid cache - query all IDs
        from app.models.catalog import Subscription

        valid_ids = set(
            db.scalars(
                select(Subscription.id).where(Subscription.id.in_(subscription_ids))
            ).all()
        )

        # Refresh cache
        self._cache = _CacheEntry(valid_ids=valid_ids)

        return valid_ids

    def invalidate(self) -> None:
        """Clear the cache."""
        self._cache = None


class BandwidthMetricsAdapter:
    """Unified adapter for bandwidth metrics operations.

    Combines VictoriaMetrics writing with subscription ID caching
    for efficient use in Celery tasks.
    """

    name = "bandwidth.metrics"

    def __init__(
        self,
        base_url: str | None = None,
        timeout: float | None = None,
        cache_ttl_seconds: float = _DEFAULT_CACHE_TTL_SECONDS,
    ):
        self._writer = VictoriaMetricsWriter(base_url=base_url, timeout=timeout)
        self._cache = SubscriptionIdCache(ttl_seconds=cache_ttl_seconds)

    def write_aggregates_batch(
        self,
        aggregates: list[BandwidthAggregate],
    ) -> WriteResult:
        """Write multiple bandwidth aggregates to VictoriaMetrics.

        Args:
            aggregates: List of bandwidth aggregates to write.

        Returns:
            WriteResult with success status and count.
        """
        return self._writer.write_aggregates_batch(aggregates)

    def filter_valid_subscription_ids(
        self,
        db: Session,
        subscription_ids: set[UUID],
    ) -> set[UUID]:
        """Filter subscription IDs to only valid (existing) ones.

        Uses caching to reduce database queries.

        Args:
            db: Database session.
            subscription_ids: Set of subscription IDs to validate.

        Returns:
            Set of valid subscription IDs.
        """
        return self._cache.filter_valid(db, subscription_ids)

    def health_check(self) -> bool:
        """Check if VictoriaMetrics is healthy."""
        return self._writer.health_check()

    def close(self) -> None:
        """Close underlying HTTP client."""
        self._writer.close()

    def invalidate_cache(self) -> None:
        """Invalidate the subscription ID cache."""
        self._cache.invalidate()


# Singleton instance
_adapter: BandwidthMetricsAdapter | None = None


def get_bandwidth_metrics_adapter() -> BandwidthMetricsAdapter:
    """Get or create the bandwidth metrics adapter singleton."""
    global _adapter
    if _adapter is None:
        _adapter = BandwidthMetricsAdapter()
        adapter_registry.register(_adapter)
    return _adapter
