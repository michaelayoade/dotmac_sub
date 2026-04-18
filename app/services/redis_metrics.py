"""Prometheus metrics for Redis circuit breaker monitoring.

Exposes metrics for:
- Circuit breaker state changes
- Connection failures
- Redis operation latencies
"""

from __future__ import annotations

import logging
import time
from collections.abc import Callable
from contextlib import contextmanager
from functools import wraps
from typing import Any, TypeVar

from prometheus_client import Counter, Gauge, Histogram

logger = logging.getLogger(__name__)

# Circuit breaker metrics
REDIS_CIRCUIT_STATE = Gauge(
    "redis_circuit_breaker_open",
    "Whether the Redis circuit breaker is open (1) or closed (0)",
)

REDIS_CIRCUIT_FAILURES = Counter(
    "redis_circuit_breaker_failures_total",
    "Total number of Redis connection failures",
)

REDIS_CIRCUIT_OPENS = Counter(
    "redis_circuit_breaker_opens_total",
    "Total number of times the circuit breaker has opened",
)

REDIS_CIRCUIT_CLOSES = Counter(
    "redis_circuit_breaker_closes_total",
    "Total number of times the circuit breaker has closed (recovered)",
)

# Operation metrics
REDIS_OPERATIONS = Counter(
    "redis_operations_total",
    "Total Redis operations",
    ["operation", "status"],
)

REDIS_OPERATION_LATENCY = Histogram(
    "redis_operation_latency_seconds",
    "Redis operation latency in seconds",
    ["operation"],
    buckets=[0.001, 0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1.0],
)


def record_circuit_open() -> None:
    """Record that the circuit breaker has opened."""
    REDIS_CIRCUIT_STATE.set(1)
    REDIS_CIRCUIT_OPENS.inc()
    logger.warning("redis_circuit_breaker_opened")


def record_circuit_close() -> None:
    """Record that the circuit breaker has closed (recovered)."""
    REDIS_CIRCUIT_STATE.set(0)
    REDIS_CIRCUIT_CLOSES.inc()
    logger.info("redis_circuit_breaker_closed")


def record_failure() -> None:
    """Record a Redis connection/operation failure."""
    REDIS_CIRCUIT_FAILURES.inc()


def record_operation(operation: str, success: bool, latency: float) -> None:
    """Record a Redis operation with its outcome and latency."""
    status = "success" if success else "failure"
    REDIS_OPERATIONS.labels(operation=operation, status=status).inc()
    REDIS_OPERATION_LATENCY.labels(operation=operation).observe(latency)


@contextmanager
def timed_operation(operation: str):
    """Context manager to time and record a Redis operation."""
    start = time.monotonic()
    success = True
    try:
        yield
    except Exception:
        success = False
        raise
    finally:
        latency = time.monotonic() - start
        record_operation(operation, success, latency)


T = TypeVar("T")


def track_redis_operation(
    operation: str,
) -> Callable[[Callable[..., T]], Callable[..., T]]:
    """Decorator to track Redis operation metrics."""

    def decorator(func: Callable[..., T]) -> Callable[..., T]:
        @wraps(func)
        def wrapper(*args: Any, **kwargs: Any) -> T:
            start = time.monotonic()
            success = True
            try:
                return func(*args, **kwargs)
            except Exception:
                success = False
                raise
            finally:
                latency = time.monotonic() - start
                record_operation(operation, success, latency)

        return wrapper

    return decorator
