"""Thread-safe SSH connection pool for OLT devices.

Eliminates connection overhead (2-3 seconds per connection) by reusing
SSH sessions within a configurable TTL. Connections are automatically
cleaned up when they expire or encounter errors.

Usage:
    from app.services.network.olt_ssh_pool import ssh_pool, pooled_ssh_connection

    # Context manager (recommended)
    with pooled_ssh_connection(olt) as (channel, policy):
        channel.send("display ont autofind all\n")
        output = read_until_prompt(channel, policy.prompt_regex)

    # Direct pool access
    conn = ssh_pool.acquire(olt)
    try:
        # ... use conn.channel ...
    finally:
        ssh_pool.release(conn)
"""

from __future__ import annotations

import atexit
import logging
import threading
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING

from paramiko.channel import Channel
from paramiko.ssh_exception import SSHException
from paramiko.transport import Transport

if TYPE_CHECKING:
    from app.models.network import OLTDevice
    from app.services.network.olt_vendor_adapters import OltSshPolicy

logger = logging.getLogger(__name__)

# Default pool settings
DEFAULT_MAX_CONNECTIONS_PER_OLT = 2
DEFAULT_CONNECTION_TTL_SECONDS = 300  # 5 minutes
DEFAULT_IDLE_TIMEOUT_SECONDS = 60  # Close idle connections after 1 minute
DEFAULT_MAX_REUSES = 100  # Recycle connection after N uses
DEFAULT_RATE_LIMIT_OPS_PER_MINUTE = 10  # Default rate limit per OLT


class RateLimitExceededError(Exception):
    """Raised when OLT operation rate limit is exceeded.

    Provides retry_after_seconds to allow callers to implement backoff.
    """

    def __init__(self, message: str, retry_after_seconds: int | None = None):
        super().__init__(message)
        self.retry_after_seconds = retry_after_seconds


@dataclass
class PooledConnection:
    """A pooled SSH connection to an OLT."""

    transport: Transport
    channel: Channel
    policy: OltSshPolicy
    olt_id: str
    olt_name: str
    created_at: datetime = field(default_factory=lambda: datetime.now(UTC))
    last_used_at: datetime = field(default_factory=lambda: datetime.now(UTC))
    use_count: int = 0
    in_use: bool = False

    def is_valid(self, ttl: timedelta, max_reuses: int) -> bool:
        """Check if connection is still usable.

        Performs comprehensive health checks:
        1. TTL expiration
        2. Reuse count limit
        3. Transport active state
        4. Channel closed state
        """
        now = datetime.now(UTC)

        # Check TTL
        if now - self.created_at > ttl:
            logger.debug(
                "Connection to %s expired (created %s ago)",
                self.olt_name,
                now - self.created_at,
            )
            return False

        # Check reuse limit
        if self.use_count >= max_reuses:
            logger.debug(
                "Connection to %s hit reuse limit (%d uses)",
                self.olt_name,
                self.use_count,
            )
            return False

        # Enhanced transport health check
        try:
            if not self.transport.is_active():
                logger.debug("Connection to %s transport is inactive", self.olt_name)
                return False
        except Exception as exc:
            logger.debug(
                "Connection to %s transport health check failed: %s",
                self.olt_name,
                exc,
            )
            return False

        # Check channel state
        try:
            if self.channel.closed:
                logger.debug("Connection to %s channel is closed", self.olt_name)
                return False
        except Exception as exc:
            logger.debug(
                "Connection to %s channel health check failed: %s",
                self.olt_name,
                exc,
            )
            return False

        return True

    def touch(self) -> None:
        """Mark connection as recently used."""
        self.last_used_at = datetime.now(UTC)
        self.use_count += 1

    def close(self) -> None:
        """Close the connection safely."""
        try:
            if self.channel:
                self.channel.close()
        except Exception:
            pass
        try:
            if self.transport:
                self.transport.close()
        except Exception:
            pass


class OltSshPool:
    """Thread-safe SSH connection pool for OLT devices.

    Maintains a pool of SSH connections per OLT device, reusing them
    to avoid the overhead of establishing new connections (typically 2-3s).

    Thread Safety:
        - Uses a global lock for pool operations
        - Connections are marked in_use while checked out
        - Multiple concurrent users can have separate connections

    Connection Lifecycle:
        1. acquire() - Get or create a connection, mark in_use
        2. Use the channel for commands
        3. release() - Return connection to pool, unmark in_use
        4. Cleanup - Expired/dead connections removed periodically
    """

    def __init__(
        self,
        max_connections_per_olt: int = DEFAULT_MAX_CONNECTIONS_PER_OLT,
        ttl_seconds: int = DEFAULT_CONNECTION_TTL_SECONDS,
        idle_timeout_seconds: int = DEFAULT_IDLE_TIMEOUT_SECONDS,
        max_reuses: int = DEFAULT_MAX_REUSES,
    ):
        self._pools: dict[str, list[PooledConnection]] = {}
        self._lock = threading.RLock()
        self._max_per_olt = max_connections_per_olt
        self._ttl = timedelta(seconds=ttl_seconds)
        self._idle_timeout = timedelta(seconds=idle_timeout_seconds)
        self._max_reuses = max_reuses
        self._stats = {
            "hits": 0,
            "misses": 0,
            "evictions": 0,
            "errors": 0,
        }

    def acquire(self, olt: OLTDevice) -> PooledConnection:
        """Acquire a connection from the pool or create a new one.

        Args:
            olt: OLT device to connect to.

        Returns:
            PooledConnection ready for use.

        Raises:
            SSHException: If connection fails.
            ValueError: If OLT credentials are missing.
            RateLimitExceededError: If rate limit is exceeded for this OLT.
        """
        # Check rate limit before acquiring connection
        self._check_rate_limit(olt)

        olt_key = str(olt.id)

        with self._lock:
            # Clean up expired connections first
            self._cleanup_pool(olt_key)

            # Try to find an available connection
            if olt_key in self._pools:
                for conn in self._pools[olt_key]:
                    if not conn.in_use and conn.is_valid(self._ttl, self._max_reuses):
                        conn.in_use = True
                        conn.touch()
                        self._stats["hits"] += 1
                        logger.debug(
                            "SSH pool hit for %s (use #%d)",
                            olt.name,
                            conn.use_count,
                        )
                        return conn

            # No available connection, create new one
            self._stats["misses"] += 1

        # Create connection outside lock to avoid blocking other threads
        logger.debug("SSH pool miss for %s, creating new connection", olt.name)
        conn = self._create_connection(olt)

        with self._lock:
            if olt_key not in self._pools:
                self._pools[olt_key] = []

            # Only pool if under limit
            if len(self._pools[olt_key]) < self._max_per_olt:
                self._pools[olt_key].append(conn)
            else:
                logger.debug(
                    "Pool full for %s (%d connections), connection will not be pooled",
                    olt.name,
                    len(self._pools[olt_key]),
                )

        return conn

    def release(self, conn: PooledConnection, *, close: bool = False) -> None:
        """Return a connection to the pool.

        Args:
            conn: Connection to release.
            close: If True, close and remove from pool instead of returning.
        """
        with self._lock:
            conn.in_use = False

            if close or not conn.is_valid(self._ttl, self._max_reuses):
                # Remove from pool and close
                if conn.olt_id in self._pools:
                    try:
                        self._pools[conn.olt_id].remove(conn)
                    except ValueError:
                        pass  # Not in pool
                conn.close()
                self._stats["evictions"] += 1
                logger.debug("Closed connection to %s", conn.olt_name)

    def invalidate(self, olt_id: str) -> int:
        """Close and remove all connections for an OLT.

        Use this when an OLT's credentials change or on persistent errors.

        Args:
            olt_id: UUID string of the OLT.

        Returns:
            Number of connections closed.
        """
        with self._lock:
            if olt_id not in self._pools:
                return 0

            connections = self._pools.pop(olt_id)
            for conn in connections:
                conn.close()

            logger.info("Invalidated %d pooled connections for OLT %s", len(connections), olt_id)
            return len(connections)

    def close_all(self) -> None:
        """Close all pooled connections (for shutdown)."""
        with self._lock:
            total = 0
            for olt_id, connections in self._pools.items():
                for conn in connections:
                    conn.close()
                    total += 1
            self._pools.clear()
            logger.info("Closed %d pooled SSH connections", total)

    def get_stats(self) -> dict:
        """Get pool statistics."""
        with self._lock:
            total_connections = sum(len(conns) for conns in self._pools.values())
            in_use = sum(
                1 for conns in self._pools.values() for c in conns if c.in_use
            )
            return {
                **self._stats,
                "total_connections": total_connections,
                "in_use": in_use,
                "olts_pooled": len(self._pools),
            }

    def _check_rate_limit(self, olt: OLTDevice) -> None:
        """Check if rate limit allows this operation.

        Args:
            olt: OLT device to check rate limit for.

        Raises:
            RateLimitExceededError: If rate limit is exceeded.
        """
        from app.services.rate_limiter_adapter import allow_operation

        olt_key = str(olt.id)

        # Get per-OLT rate limit from model, or use default
        ops_per_minute = getattr(
            olt, "rate_limit_ops_per_minute", None
        ) or DEFAULT_RATE_LIMIT_OPS_PER_MINUTE

        decision = allow_operation(
            key=f"olt_ssh:{olt_key}",
            limit=ops_per_minute,
            window_seconds=60,
        )

        if not decision.allowed:
            logger.warning(
                "OLT %s rate limit exceeded (%d ops/min). Retry after %ds",
                olt.name,
                ops_per_minute,
                decision.retry_after_seconds or 0,
            )
            raise RateLimitExceededError(
                f"OLT {olt.name} rate limit exceeded ({ops_per_minute}/min). "
                f"Retry after {decision.retry_after_seconds}s",
                retry_after_seconds=decision.retry_after_seconds,
            )

    def _cleanup_pool(self, olt_key: str) -> None:
        """Remove invalid connections from an OLT's pool (must hold lock)."""
        if olt_key not in self._pools:
            return

        valid = []
        for conn in self._pools[olt_key]:
            if conn.in_use:
                valid.append(conn)
            elif conn.is_valid(self._ttl, self._max_reuses):
                # Check idle timeout for unused connections
                now = datetime.now(UTC)
                if now - conn.last_used_at > self._idle_timeout:
                    conn.close()
                    self._stats["evictions"] += 1
                    logger.debug("Evicted idle connection to %s", conn.olt_name)
                else:
                    valid.append(conn)
            else:
                conn.close()
                self._stats["evictions"] += 1

        self._pools[olt_key] = valid

    def _create_connection(self, olt: OLTDevice) -> PooledConnection:
        """Create a new SSH connection to an OLT."""
        from app.services.network.olt_ssh import _open_shell

        transport, channel, policy = _open_shell(olt)

        conn = PooledConnection(
            transport=transport,
            channel=channel,
            policy=policy,
            olt_id=str(olt.id),
            olt_name=olt.name,
        )
        conn.in_use = True
        conn.touch()

        return conn


# Global pool instance
ssh_pool = OltSshPool()

# Register cleanup on process exit
atexit.register(ssh_pool.close_all)


@contextmanager
def pooled_ssh_connection(olt: OLTDevice):
    """Context manager for pooled SSH connections.

    Automatically acquires and releases connections, closing on error.

    Usage:
        with pooled_ssh_connection(olt) as (channel, policy):
            channel.send("display version\n")
            output = _read_until_prompt(channel, policy.prompt_regex)

    Yields:
        Tuple of (Channel, OltSshPolicy) for the connection.
    """
    conn = ssh_pool.acquire(olt)
    close_on_exit = False
    try:
        yield conn.channel, conn.policy
    except (SSHException, OSError, TimeoutError) as e:
        # Connection error - don't return to pool
        close_on_exit = True
        ssh_pool._stats["errors"] += 1
        logger.warning("SSH connection error for %s: %s", olt.name, e)
        raise
    finally:
        ssh_pool.release(conn, close=close_on_exit)
