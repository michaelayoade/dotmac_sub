"""Centralized Redis client management with circuit breaker pattern.

Provides a resilient Redis connection layer that:
- Maintains a shared connection pool
- Implements circuit breaker to prevent retry storms
- Provides health check capabilities
- Gracefully degrades when Redis is unavailable

Usage:
    from app.services.redis_client import get_redis, redis_health_check

    # Get client (returns None if unavailable)
    client = get_redis()
    if client:
        client.get("key")

    # Check health
    status = redis_health_check()
    if status["available"]:
        print(f"Redis {status['version']} is up")
"""

from __future__ import annotations

import logging
import os
import threading
import time
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

import redis
from redis.exceptions import RedisError

if TYPE_CHECKING:
    pass

logger = logging.getLogger(__name__)

# Circuit breaker configuration
_CIRCUIT_OPEN_DURATION = 30  # seconds to wait before retrying after failure
_MAX_FAILURES_BEFORE_OPEN = 3  # failures before circuit opens
_HEALTH_CHECK_INTERVAL = 5  # minimum seconds between health checks


@dataclass
class CircuitBreakerState:
    """Tracks circuit breaker state for Redis connections."""

    failures: int = 0
    last_failure_time: float = 0.0
    circuit_open: bool = False
    last_success_time: float = 0.0

    def record_failure(self) -> None:
        """Record a connection failure."""
        from app.services.redis_metrics import record_circuit_open, record_failure

        self.failures += 1
        self.last_failure_time = time.monotonic()
        record_failure()
        if self.failures >= _MAX_FAILURES_BEFORE_OPEN and not self.circuit_open:
            self.circuit_open = True
            record_circuit_open()
            logger.warning(
                "Redis circuit breaker OPEN after %d failures", self.failures
            )

    def record_success(self) -> None:
        """Record a successful connection."""
        from app.services.redis_metrics import record_circuit_close

        was_open = self.circuit_open
        self.failures = 0
        self.circuit_open = False
        self.last_success_time = time.monotonic()
        if was_open:
            record_circuit_close()
            logger.info("Redis circuit breaker CLOSED - connection restored")

    def should_attempt(self) -> bool:
        """Check if we should attempt a connection."""
        if not self.circuit_open:
            return True
        # Check if enough time has passed to retry
        elapsed = time.monotonic() - self.last_failure_time
        if elapsed >= _CIRCUIT_OPEN_DURATION:
            logger.info("Redis circuit breaker attempting retry after %.1fs", elapsed)
            return True
        return False


# Global state
_redis_client: redis.Redis | None = None
_circuit_state = CircuitBreakerState()
_lock = threading.Lock()
_last_health_check: dict[str, Any] = {}


def _get_redis_url() -> str:
    """Get Redis URL from environment."""
    return os.environ.get("REDIS_URL", "redis://localhost:6379/0")


def get_redis(force_reconnect: bool = False) -> redis.Redis | None:
    """Get a shared Redis client with circuit breaker protection.

    Args:
        force_reconnect: If True, attempt reconnection even if circuit is open

    Returns:
        Redis client if available, None otherwise
    """
    global _redis_client, _circuit_state

    with _lock:
        # Return existing client if healthy
        if _redis_client is not None and not force_reconnect:
            try:
                _redis_client.ping()
                return _redis_client
            except RedisError:
                _redis_client = None
                _circuit_state.record_failure()

        # Check circuit breaker
        if not force_reconnect and not _circuit_state.should_attempt():
            return None

        # Attempt connection
        try:
            client = redis.Redis.from_url(
                _get_redis_url(),
                decode_responses=True,
                socket_connect_timeout=3,
                socket_timeout=3,
            )
            client.ping()
            _redis_client = client
            _circuit_state.record_success()
            return client
        except RedisError as exc:
            logger.warning("Redis connection failed: %s", exc)
            _circuit_state.record_failure()
            return None


def reset_redis_client() -> None:
    """Reset the Redis client state (for testing or recovery)."""
    global _redis_client, _circuit_state
    with _lock:
        _redis_client = None
        _circuit_state = CircuitBreakerState()


def redis_health_check(force: bool = False) -> dict[str, Any]:
    """Perform a Redis health check.

    Args:
        force: If True, bypass cache and check immediately

    Returns:
        Dict with health status information
    """
    global _last_health_check

    now = time.monotonic()

    # Return cached result if recent enough
    if not force and _last_health_check:
        last_check_time = _last_health_check.get("_check_time", 0)
        if now - last_check_time < _HEALTH_CHECK_INTERVAL:
            return _last_health_check

    result: dict[str, Any] = {
        "available": False,
        "circuit_open": _circuit_state.circuit_open,
        "failure_count": _circuit_state.failures,
        "checked_at": datetime.now(UTC).isoformat(),
        "_check_time": now,
    }

    try:
        client = get_redis(force_reconnect=force)
        if client is None:
            result["error"] = "No client available (circuit open or connection failed)"
            _last_health_check = result
            return result

        # Get detailed info
        start = time.monotonic()
        client.ping()
        response_ms = (time.monotonic() - start) * 1000

        info: dict[str, Any] = client.info()  # type: ignore[assignment]
        result.update(
            {
                "available": True,
                "response_ms": round(response_ms, 2),
                "version": str(info.get("redis_version", "unknown")),
                "uptime_seconds": int(info.get("uptime_in_seconds", 0)),
                "connected_clients": int(info.get("connected_clients", 0)),
                "used_memory_human": str(info.get("used_memory_human", "unknown")),
                "used_memory_peak_human": str(
                    info.get("used_memory_peak_human", "unknown")
                ),
                "total_commands_processed": int(
                    info.get("total_commands_processed", 0)
                ),
                "keyspace_hits": int(info.get("keyspace_hits", 0)),
                "keyspace_misses": int(info.get("keyspace_misses", 0)),
            }
        )

        # Calculate hit rate
        hits: int = result["keyspace_hits"]
        misses: int = result["keyspace_misses"]
        if hits + misses > 0:
            result["hit_rate_percent"] = round(hits / (hits + misses) * 100, 2)

    except RedisError as exc:
        result["error"] = str(exc)[:200]
        logger.warning("Redis health check failed: %s", exc)

    _last_health_check = result
    return result


def get_circuit_state() -> dict[str, Any]:
    """Get the current circuit breaker state."""
    return {
        "circuit_open": _circuit_state.circuit_open,
        "failure_count": _circuit_state.failures,
        "last_failure_age_seconds": (
            round(time.monotonic() - _circuit_state.last_failure_time, 1)
            if _circuit_state.last_failure_time > 0
            else None
        ),
        "last_success_age_seconds": (
            round(time.monotonic() - _circuit_state.last_success_time, 1)
            if _circuit_state.last_success_time > 0
            else None
        ),
        "retry_after_seconds": (
            max(
                0,
                _CIRCUIT_OPEN_DURATION
                - (time.monotonic() - _circuit_state.last_failure_time),
            )
            if _circuit_state.circuit_open
            else 0
        ),
    }


# Convenience functions for common operations with graceful degradation


def safe_get(key: str, default: Any = None) -> Any:
    """Get a value from Redis with graceful degradation.

    Returns default if Redis is unavailable.
    """
    client = get_redis()
    if client is None:
        return default
    try:
        value = client.get(key)
        return value if value is not None else default
    except RedisError as exc:
        logger.debug("Redis GET failed for %s: %s", key, exc)
        return default


def safe_set(key: str, value: str, ttl: int | None = None) -> bool:
    """Set a value in Redis with graceful degradation.

    Returns False if Redis is unavailable.
    """
    client = get_redis()
    if client is None:
        return False
    try:
        if ttl:
            client.setex(key, ttl, value)
        else:
            client.set(key, value)
        return True
    except RedisError as exc:
        logger.debug("Redis SET failed for %s: %s", key, exc)
        return False


def safe_delete(key: str) -> bool:
    """Delete a key from Redis with graceful degradation.

    Returns False if Redis is unavailable.
    """
    client = get_redis()
    if client is None:
        return False
    try:
        client.delete(key)
        return True
    except RedisError as exc:
        logger.debug("Redis DELETE failed for %s: %s", key, exc)
        return False
