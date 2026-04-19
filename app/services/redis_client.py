"""Centralized Redis client management with circuit breaker pattern.

Provides a resilient Redis connection layer that:
- Maintains a shared connection pool
- Implements circuit breaker to prevent retry storms
- Provides health check capabilities
- Gracefully degrades when Redis is unavailable
- Falls back to in-memory cache when Redis is down

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

    # Safe operations with automatic fallback to in-memory cache
    from app.services.redis_client import safe_get, safe_set
    value = safe_get("key", default="fallback")  # Uses memory cache if Redis down
    safe_set("key", "value", ttl=60)  # Writes to both Redis and memory cache
"""

from __future__ import annotations

import logging
import os
import threading
import time
from collections import OrderedDict
from dataclasses import dataclass, field
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

# In-memory fallback cache configuration
_FALLBACK_CACHE_MAX_SIZE = 1000  # Maximum entries in fallback cache
_FALLBACK_CACHE_DEFAULT_TTL = 300  # Default TTL for fallback cache entries (5 min)


@dataclass
class CacheEntry:
    """An entry in the fallback cache with expiration."""

    value: Any
    expires_at: float  # monotonic timestamp

    def is_expired(self) -> bool:
        return time.monotonic() > self.expires_at


class FallbackCache:
    """Thread-safe in-memory LRU cache with TTL for Redis fallback.

    Used when Redis is unavailable to provide degraded but functional caching.
    """

    def __init__(self, max_size: int = _FALLBACK_CACHE_MAX_SIZE) -> None:
        self._cache: OrderedDict[str, CacheEntry] = OrderedDict()
        self._max_size = max_size
        self._lock = threading.Lock()
        self._hits = 0
        self._misses = 0

    def get(self, key: str) -> tuple[Any, bool]:
        """Get a value from the cache.

        Returns:
            Tuple of (value, found). If found is False, value is None.
        """
        with self._lock:
            entry = self._cache.get(key)
            if entry is None:
                self._misses += 1
                return None, False
            if entry.is_expired():
                del self._cache[key]
                self._misses += 1
                return None, False
            # Move to end (most recently used)
            self._cache.move_to_end(key)
            self._hits += 1
            return entry.value, True

    def set(self, key: str, value: Any, ttl: int | None = None) -> None:
        """Set a value in the cache with optional TTL."""
        ttl = ttl or _FALLBACK_CACHE_DEFAULT_TTL
        expires_at = time.monotonic() + ttl
        with self._lock:
            # Remove if exists to update position
            if key in self._cache:
                del self._cache[key]
            # Evict oldest entries if at capacity
            while len(self._cache) >= self._max_size:
                self._cache.popitem(last=False)
            self._cache[key] = CacheEntry(value=value, expires_at=expires_at)

    def delete(self, key: str) -> bool:
        """Delete a key from the cache."""
        with self._lock:
            if key in self._cache:
                del self._cache[key]
                return True
            return False

    def clear(self) -> None:
        """Clear all entries from the cache."""
        with self._lock:
            self._cache.clear()
            self._hits = 0
            self._misses = 0

    def cleanup_expired(self) -> int:
        """Remove expired entries. Returns count of removed entries."""
        removed = 0
        with self._lock:
            expired_keys = [
                k for k, v in self._cache.items() if v.is_expired()
            ]
            for key in expired_keys:
                del self._cache[key]
                removed += 1
        return removed

    def stats(self) -> dict[str, Any]:
        """Get cache statistics."""
        with self._lock:
            total = self._hits + self._misses
            return {
                "size": len(self._cache),
                "max_size": self._max_size,
                "hits": self._hits,
                "misses": self._misses,
                "hit_rate": round(self._hits / total * 100, 2) if total > 0 else 0.0,
            }


# Global fallback cache instance
_fallback_cache = FallbackCache()


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
    """Get the current circuit breaker state including fallback cache stats."""
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
        "fallback_cache": _fallback_cache.stats(),
    }


# Convenience functions for common operations with graceful degradation


def safe_get(key: str, default: Any = None, *, use_fallback: bool = True) -> Any:
    """Get a value from Redis with graceful degradation.

    Args:
        key: The key to retrieve.
        default: Value to return if key not found.
        use_fallback: If True, use in-memory fallback cache when Redis unavailable.

    Returns:
        The value from Redis, fallback cache, or default.
    """
    client = get_redis()
    if client is None:
        if use_fallback:
            value, found = _fallback_cache.get(key)
            if found:
                logger.debug("Fallback cache HIT for %s (Redis unavailable)", key)
                return value
        return default
    try:
        value = client.get(key)
        if value is not None:
            # Update fallback cache with fresh data from Redis
            if use_fallback:
                _fallback_cache.set(key, value)
            return value
        return default
    except RedisError as exc:
        logger.debug("Redis GET failed for %s: %s", key, exc)
        if use_fallback:
            value, found = _fallback_cache.get(key)
            if found:
                logger.debug("Fallback cache HIT for %s (Redis error)", key)
                return value
        return default


def safe_set(
    key: str, value: str, ttl: int | None = None, *, use_fallback: bool = True
) -> bool:
    """Set a value in Redis with graceful degradation.

    Args:
        key: The key to set.
        value: The value to store.
        ttl: Time-to-live in seconds.
        use_fallback: If True, also write to in-memory fallback cache.

    Returns:
        True if written to Redis, False if only fallback or failed.
    """
    # Always update fallback cache for resilience
    if use_fallback:
        _fallback_cache.set(key, value, ttl)

    client = get_redis()
    if client is None:
        logger.debug("Redis unavailable, wrote %s to fallback cache only", key)
        return False
    try:
        if ttl:
            client.setex(key, ttl, value)
        else:
            client.set(key, value)
        return True
    except RedisError as exc:
        logger.debug("Redis SET failed for %s: %s (fallback cache updated)", key, exc)
        return False


def safe_delete(key: str, *, use_fallback: bool = True) -> bool:
    """Delete a key from Redis with graceful degradation.

    Args:
        key: The key to delete.
        use_fallback: If True, also delete from in-memory fallback cache.

    Returns:
        True if deleted from Redis, False otherwise.
    """
    # Always remove from fallback cache
    if use_fallback:
        _fallback_cache.delete(key)

    client = get_redis()
    if client is None:
        return False
    try:
        client.delete(key)
        return True
    except RedisError as exc:
        logger.debug("Redis DELETE failed for %s: %s", key, exc)
        return False


def get_fallback_cache_stats() -> dict[str, Any]:
    """Get statistics about the fallback cache."""
    return _fallback_cache.stats()


def clear_fallback_cache() -> None:
    """Clear the fallback cache (for testing or manual recovery)."""
    _fallback_cache.clear()
    logger.info("Fallback cache cleared")
