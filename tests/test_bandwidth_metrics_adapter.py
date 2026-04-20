"""Tests for bandwidth metrics adapter."""

from datetime import UTC, datetime
from unittest.mock import MagicMock, patch
from uuid import uuid4

import pytest

from app.services.bandwidth_metrics_adapter import (
    BandwidthAggregate,
    BandwidthMetricsAdapter,
    SubscriptionIdCache,
    VictoriaMetricsWriter,
    WriteResult,
    get_bandwidth_metrics_adapter,
)


class TestBandwidthAggregate:
    def test_dataclass_fields(self) -> None:
        ts = datetime.now(UTC)
        agg = BandwidthAggregate(
            subscription_id="sub-123",
            nas_device_id="nas-456",
            timestamp=ts,
            rx_avg=1000.0,
            tx_avg=500.0,
            rx_max=2000.0,
            tx_max=1000.0,
            sample_count=10,
        )
        assert agg.subscription_id == "sub-123"
        assert agg.nas_device_id == "nas-456"
        assert agg.timestamp == ts
        assert agg.rx_avg == 1000.0
        assert agg.tx_avg == 500.0
        assert agg.rx_max == 2000.0
        assert agg.tx_max == 1000.0
        assert agg.sample_count == 10

    def test_nas_device_id_optional(self) -> None:
        agg = BandwidthAggregate(
            subscription_id="sub-123",
            nas_device_id=None,
            timestamp=datetime.now(UTC),
            rx_avg=0.0,
            tx_avg=0.0,
            rx_max=0.0,
            tx_max=0.0,
            sample_count=0,
        )
        assert agg.nas_device_id is None


class TestWriteResult:
    def test_success_result(self) -> None:
        result = WriteResult(success=True, written=5)
        assert result.success is True
        assert result.written == 5
        assert result.error is None

    def test_error_result(self) -> None:
        result = WriteResult(success=False, written=0, error="Connection failed")
        assert result.success is False
        assert result.written == 0
        assert result.error == "Connection failed"


class TestVictoriaMetricsWriter:
    def test_format_aggregate_lines_with_nas_device(self) -> None:
        writer = VictoriaMetricsWriter()
        ts = datetime(2024, 1, 15, 12, 0, 0, tzinfo=UTC)
        agg = BandwidthAggregate(
            subscription_id="sub-123",
            nas_device_id="nas-456",
            timestamp=ts,
            rx_avg=1000.0,
            tx_avg=500.0,
            rx_max=2000.0,
            tx_max=1000.0,
            sample_count=10,
        )
        lines = writer._format_aggregate_lines(agg)
        assert len(lines) == 5
        assert 'subscription_id="sub-123"' in lines[0]
        assert 'nas_device_id="nas-456"' in lines[0]

    def test_format_aggregate_lines_without_nas_device(self) -> None:
        writer = VictoriaMetricsWriter()
        ts = datetime(2024, 1, 15, 12, 0, 0, tzinfo=UTC)
        agg = BandwidthAggregate(
            subscription_id="sub-123",
            nas_device_id=None,
            timestamp=ts,
            rx_avg=1000.0,
            tx_avg=500.0,
            rx_max=2000.0,
            tx_max=1000.0,
            sample_count=10,
        )
        lines = writer._format_aggregate_lines(agg)
        assert len(lines) == 5
        assert 'subscription_id="sub-123"' in lines[0]
        assert "nas_device_id" not in lines[0]

    def test_write_empty_batch_returns_success(self) -> None:
        writer = VictoriaMetricsWriter()
        result = writer.write_aggregates_batch([])
        assert result.success is True
        assert result.written == 0

    @patch("app.services.bandwidth_metrics_adapter.httpx.Client")
    def test_write_batch_success(self, mock_client_class) -> None:
        mock_client = MagicMock()
        mock_response = MagicMock()
        mock_response.raise_for_status = MagicMock()
        mock_client.post.return_value = mock_response
        mock_client.is_closed = False
        mock_client_class.return_value = mock_client

        writer = VictoriaMetricsWriter()
        writer._client = mock_client

        ts = datetime.now(UTC)
        batch = [
            BandwidthAggregate(
                subscription_id="sub-1",
                nas_device_id=None,
                timestamp=ts,
                rx_avg=100.0,
                tx_avg=50.0,
                rx_max=200.0,
                tx_max=100.0,
                sample_count=5,
            ),
            BandwidthAggregate(
                subscription_id="sub-2",
                nas_device_id="nas-1",
                timestamp=ts,
                rx_avg=200.0,
                tx_avg=100.0,
                rx_max=400.0,
                tx_max=200.0,
                sample_count=10,
            ),
        ]

        result = writer.write_aggregates_batch(batch)

        assert result.success is True
        assert result.written == 2
        mock_client.post.assert_called_once()

    @patch("app.services.bandwidth_metrics_adapter.httpx.Client")
    def test_write_batch_failure(self, mock_client_class) -> None:
        import httpx

        mock_client = MagicMock()
        mock_client.post.side_effect = httpx.HTTPError("Connection refused")
        mock_client.is_closed = False
        mock_client_class.return_value = mock_client

        writer = VictoriaMetricsWriter()
        writer._client = mock_client

        ts = datetime.now(UTC)
        batch = [
            BandwidthAggregate(
                subscription_id="sub-1",
                nas_device_id=None,
                timestamp=ts,
                rx_avg=100.0,
                tx_avg=50.0,
                rx_max=200.0,
                tx_max=100.0,
                sample_count=5,
            ),
        ]

        result = writer.write_aggregates_batch(batch)

        assert result.success is False
        assert result.written == 0
        assert "Connection refused" in str(result.error)


class TestSubscriptionIdCache:
    def test_filter_empty_set(self, db_session) -> None:
        cache = SubscriptionIdCache()
        result = cache.filter_valid(db_session, set())
        assert result == set()

    def test_filter_valid_ids(self, db_session, subscription) -> None:
        cache = SubscriptionIdCache()
        test_ids = {subscription.id, uuid4()}

        valid = cache.filter_valid(db_session, test_ids)

        assert subscription.id in valid
        assert len(valid) == 1

    def test_cache_hit_avoids_query(self, db_session, subscription) -> None:
        cache = SubscriptionIdCache(ttl_seconds=300)

        # First call populates cache
        valid1 = cache.filter_valid(db_session, {subscription.id})
        assert subscription.id in valid1

        # Second call should use cache
        valid2 = cache.filter_valid(db_session, {subscription.id})
        assert valid2 == valid1

    def test_invalidate_clears_cache(self, db_session, subscription) -> None:
        cache = SubscriptionIdCache()

        # Populate cache
        cache.filter_valid(db_session, {subscription.id})
        assert cache._cache is not None

        # Invalidate
        cache.invalidate()
        assert cache._cache is None


class TestBandwidthMetricsAdapter:
    def test_singleton_returns_same_instance(self) -> None:
        adapter1 = get_bandwidth_metrics_adapter()
        adapter2 = get_bandwidth_metrics_adapter()
        assert adapter1 is adapter2

    def test_adapter_has_all_methods(self) -> None:
        adapter = BandwidthMetricsAdapter()
        assert hasattr(adapter, "write_aggregates_batch")
        assert hasattr(adapter, "filter_valid_subscription_ids")
        assert hasattr(adapter, "health_check")
        assert hasattr(adapter, "close")
        assert hasattr(adapter, "invalidate_cache")

    def test_filter_valid_subscription_ids(self, db_session, subscription) -> None:
        adapter = BandwidthMetricsAdapter()
        test_ids = {subscription.id, uuid4()}

        valid = adapter.filter_valid_subscription_ids(db_session, test_ids)

        assert subscription.id in valid
        assert len(valid) == 1

    @patch("app.services.bandwidth_metrics_adapter.httpx.Client")
    def test_write_aggregates_batch(self, mock_client_class) -> None:
        mock_client = MagicMock()
        mock_response = MagicMock()
        mock_response.raise_for_status = MagicMock()
        mock_client.post.return_value = mock_response
        mock_client.is_closed = False
        mock_client_class.return_value = mock_client

        adapter = BandwidthMetricsAdapter()
        adapter._writer._client = mock_client

        ts = datetime.now(UTC)
        batch = [
            BandwidthAggregate(
                subscription_id="sub-1",
                nas_device_id=None,
                timestamp=ts,
                rx_avg=100.0,
                tx_avg=50.0,
                rx_max=200.0,
                tx_max=100.0,
                sample_count=5,
            ),
        ]

        result = adapter.write_aggregates_batch(batch)

        assert result.success is True
        assert result.written == 1
