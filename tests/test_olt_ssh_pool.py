"""Tests for OLT SSH connection pool with rate limiting and health checks."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from app.services.network.olt_ssh_pool import (
    DEFAULT_RATE_LIMIT_OPS_PER_MINUTE,
    OltSshPool,
    PooledConnection,
    RateLimitExceededError,
)


@pytest.fixture
def mock_transport():
    """Create a mock Paramiko transport."""
    transport = MagicMock()
    transport.is_active.return_value = True
    return transport


@pytest.fixture
def mock_channel():
    """Create a mock Paramiko channel."""
    channel = MagicMock()
    channel.closed = False
    return channel


@pytest.fixture
def mock_policy():
    """Create a mock SSH policy."""
    return MagicMock()


@pytest.fixture
def pooled_conn(mock_transport, mock_channel, mock_policy):
    """Create a test PooledConnection."""
    return PooledConnection(
        transport=mock_transport,
        channel=mock_channel,
        policy=mock_policy,
        olt_id="test-olt-123",
        olt_name="Test-OLT",
        created_at=datetime.now(UTC),
        last_used_at=datetime.now(UTC),
        use_count=0,
        in_use=False,
    )


@pytest.fixture
def mock_olt():
    """Create a mock OLT device."""
    return SimpleNamespace(
        id="test-olt-456",
        name="Test-OLT",
        mgmt_ip="192.168.1.1",
        ssh_username="admin",
        ssh_password="enc:password",
        ssh_port=22,
        rate_limit_ops_per_minute=10,
    )


@pytest.fixture
def mock_olt_no_rate_limit():
    """Create a mock OLT without rate limit configured."""
    olt = SimpleNamespace(
        id="test-olt-789",
        name="NoLimit-OLT",
        mgmt_ip="192.168.1.2",
        ssh_username="admin",
        ssh_password="enc:password",
        ssh_port=22,
    )
    # Simulate attribute not being present
    return olt


class TestPooledConnectionIsValid:
    """Tests for PooledConnection.is_valid() health checks."""

    def test_valid_connection(self, pooled_conn):
        ttl = timedelta(minutes=5)
        max_reuses = 100
        assert pooled_conn.is_valid(ttl, max_reuses) is True

    def test_expired_ttl(self, pooled_conn):
        pooled_conn.created_at = datetime.now(UTC) - timedelta(minutes=10)
        ttl = timedelta(minutes=5)
        max_reuses = 100
        assert pooled_conn.is_valid(ttl, max_reuses) is False

    def test_reuse_limit_exceeded(self, pooled_conn):
        pooled_conn.use_count = 101
        ttl = timedelta(minutes=5)
        max_reuses = 100
        assert pooled_conn.is_valid(ttl, max_reuses) is False

    def test_transport_inactive(self, pooled_conn, mock_transport):
        mock_transport.is_active.return_value = False
        ttl = timedelta(minutes=5)
        max_reuses = 100
        assert pooled_conn.is_valid(ttl, max_reuses) is False

    def test_transport_is_active_raises_exception(self, pooled_conn, mock_transport):
        mock_transport.is_active.side_effect = Exception("Transport error")
        ttl = timedelta(minutes=5)
        max_reuses = 100
        assert pooled_conn.is_valid(ttl, max_reuses) is False

    def test_channel_closed(self, pooled_conn, mock_channel):
        mock_channel.closed = True
        ttl = timedelta(minutes=5)
        max_reuses = 100
        assert pooled_conn.is_valid(ttl, max_reuses) is False

    def test_channel_closed_check_raises_exception(self, pooled_conn, mock_channel):
        type(mock_channel).closed = property(
            lambda self: (_ for _ in ()).throw(Exception("Channel error"))
        )
        ttl = timedelta(minutes=5)
        max_reuses = 100
        assert pooled_conn.is_valid(ttl, max_reuses) is False


class TestPooledConnectionTouch:
    """Tests for PooledConnection.touch() method."""

    def test_touch_updates_last_used(self, pooled_conn):
        old_time = pooled_conn.last_used_at
        pooled_conn.touch()
        assert pooled_conn.last_used_at >= old_time

    def test_touch_increments_use_count(self, pooled_conn):
        initial_count = pooled_conn.use_count
        pooled_conn.touch()
        assert pooled_conn.use_count == initial_count + 1


class TestPooledConnectionClose:
    """Tests for PooledConnection.close() method."""

    def test_close_closes_channel_and_transport(self, pooled_conn, mock_channel, mock_transport):
        pooled_conn.close()
        mock_channel.close.assert_called_once()
        mock_transport.close.assert_called_once()

    def test_close_handles_channel_exception(self, pooled_conn, mock_channel, mock_transport):
        mock_channel.close.side_effect = Exception("Close error")
        # Should not raise
        pooled_conn.close()
        mock_transport.close.assert_called_once()

    def test_close_handles_transport_exception(self, pooled_conn, mock_channel, mock_transport):
        mock_transport.close.side_effect = Exception("Close error")
        # Should not raise
        pooled_conn.close()


class TestRateLimitExceededError:
    """Tests for RateLimitExceededError exception."""

    def test_error_message(self):
        error = RateLimitExceededError("Rate limit exceeded", retry_after_seconds=30)
        assert str(error) == "Rate limit exceeded"
        assert error.retry_after_seconds == 30

    def test_error_without_retry(self):
        error = RateLimitExceededError("Rate limit exceeded")
        assert str(error) == "Rate limit exceeded"
        assert error.retry_after_seconds is None


class TestOltSshPoolRateLimiting:
    """Tests for OLT SSH pool rate limiting."""

    def test_acquire_within_rate_limit(self, mock_olt):
        pool = OltSshPool()

        with patch("app.services.rate_limiter_adapter.allow_operation") as mock_allow:
            mock_allow.return_value = SimpleNamespace(
                allowed=True,
                remaining=9,
                retry_after_seconds=None,
            )
            with patch.object(pool, "_create_connection") as mock_create:
                mock_create.return_value = MagicMock(
                    olt_id=str(mock_olt.id),
                    is_valid=MagicMock(return_value=True),
                    in_use=False,
                )
                # Should not raise
                pool._check_rate_limit(mock_olt)

    def test_acquire_rate_limit_exceeded(self, mock_olt):
        pool = OltSshPool()

        with patch("app.services.rate_limiter_adapter.allow_operation") as mock_allow:
            mock_allow.return_value = SimpleNamespace(
                allowed=False,
                remaining=0,
                retry_after_seconds=45,
            )
            with pytest.raises(RateLimitExceededError) as exc_info:
                pool._check_rate_limit(mock_olt)

            assert exc_info.value.retry_after_seconds == 45
            assert "rate limit exceeded" in str(exc_info.value).lower()
            assert "10/min" in str(exc_info.value)

    def test_rate_limit_uses_olt_setting(self, mock_olt):
        pool = OltSshPool()
        mock_olt.rate_limit_ops_per_minute = 20

        with patch("app.services.rate_limiter_adapter.allow_operation") as mock_allow:
            mock_allow.return_value = SimpleNamespace(
                allowed=True,
                remaining=19,
                retry_after_seconds=None,
            )
            pool._check_rate_limit(mock_olt)

            # Verify the limit passed to allow_operation
            mock_allow.assert_called_once()
            call_kwargs = mock_allow.call_args[1]
            assert call_kwargs["limit"] == 20

    def test_rate_limit_uses_default_when_not_set(self, mock_olt_no_rate_limit):
        pool = OltSshPool()

        with patch("app.services.rate_limiter_adapter.allow_operation") as mock_allow:
            mock_allow.return_value = SimpleNamespace(
                allowed=True,
                remaining=9,
                retry_after_seconds=None,
            )
            pool._check_rate_limit(mock_olt_no_rate_limit)

            # Verify default limit is used
            mock_allow.assert_called_once()
            call_kwargs = mock_allow.call_args[1]
            assert call_kwargs["limit"] == DEFAULT_RATE_LIMIT_OPS_PER_MINUTE


class TestOltSshPoolStats:
    """Tests for OLT SSH pool statistics."""

    def test_get_stats(self):
        pool = OltSshPool()
        stats = pool.get_stats()

        assert "hits" in stats
        assert "misses" in stats
        assert "evictions" in stats
        assert "errors" in stats
        assert "total_connections" in stats
        assert "in_use" in stats
        assert "olts_pooled" in stats


class TestOltSshPoolInvalidate:
    """Tests for OLT SSH pool invalidation."""

    def test_invalidate_returns_zero_when_no_connections(self):
        pool = OltSshPool()
        count = pool.invalidate("nonexistent-olt")
        assert count == 0

    def test_invalidate_closes_connections(self, mock_transport, mock_channel, mock_policy):
        pool = OltSshPool()
        olt_id = "test-olt"

        # Manually add a connection to the pool
        conn = PooledConnection(
            transport=mock_transport,
            channel=mock_channel,
            policy=mock_policy,
            olt_id=olt_id,
            olt_name="Test-OLT",
        )
        pool._pools[olt_id] = [conn]

        count = pool.invalidate(olt_id)
        assert count == 1
        assert olt_id not in pool._pools
        mock_channel.close.assert_called()
        mock_transport.close.assert_called()


class TestOltSshPoolCloseAll:
    """Tests for OLT SSH pool close_all."""

    def test_close_all_clears_pools(self, mock_transport, mock_channel, mock_policy):
        pool = OltSshPool()

        # Add connections to multiple OLTs
        for i in range(3):
            conn = PooledConnection(
                transport=mock_transport,
                channel=mock_channel,
                policy=mock_policy,
                olt_id=f"olt-{i}",
                olt_name=f"OLT-{i}",
            )
            pool._pools[f"olt-{i}"] = [conn]

        pool.close_all()

        assert len(pool._pools) == 0
