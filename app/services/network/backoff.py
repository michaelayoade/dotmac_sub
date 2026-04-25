"""Exponential backoff utilities for polling and retry operations.

Provides configurable backoff with jitter for network polling,
bootstrap waits, and task retries.

Usage:
    from app.services.network.backoff import BackoffConfig, ExponentialBackoff

    # Simple iteration
    backoff = ExponentialBackoff()
    for attempt, delay in backoff:
        result = poll_for_device()
        if result.found:
            break
        time.sleep(delay)

    # Custom configuration
    config = BackoffConfig(initial_delay=2.0, max_delay=60.0, multiplier=1.5)
    backoff = ExponentialBackoff(config, max_attempts=10)
"""

from __future__ import annotations

import logging
import random  # noqa: S311 - used for jitter, not cryptographic purposes
from collections.abc import Iterator
from dataclasses import dataclass

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class BackoffConfig:
    """Configuration for exponential backoff behavior.

    Attributes:
        initial_delay: Starting delay in seconds (default 2.0)
        max_delay: Maximum delay cap in seconds (default 30.0)
        multiplier: Factor to multiply delay each attempt (default 2.0)
        jitter: Random variation as fraction of delay (default 0.1 = 10%)

    Example sequence with defaults (2s initial, 2x multiplier, 30s cap):
        Attempt 1: 2s
        Attempt 2: 4s
        Attempt 3: 8s
        Attempt 4: 16s
        Attempt 5: 30s (capped)
        Attempt 6+: 30s (capped)
    """

    initial_delay: float = 2.0
    max_delay: float = 30.0
    multiplier: float = 2.0
    jitter: float = 0.1

    def __post_init__(self) -> None:
        """Validate configuration values."""
        if self.initial_delay <= 0:
            raise ValueError("initial_delay must be positive")
        if self.max_delay < self.initial_delay:
            raise ValueError("max_delay must be >= initial_delay")
        if self.multiplier < 1.0:
            raise ValueError("multiplier must be >= 1.0")
        if not 0 <= self.jitter < 1:
            raise ValueError("jitter must be in [0, 1)")


# Default configurations for common use cases
BOOTSTRAP_BACKOFF = BackoffConfig(
    initial_delay=2.0,
    max_delay=30.0,
    multiplier=2.0,
    jitter=0.1,
)

TASK_RETRY_BACKOFF = BackoffConfig(
    initial_delay=30.0,
    max_delay=240.0,
    multiplier=2.0,
    jitter=0.15,
)

FAST_POLL_BACKOFF = BackoffConfig(
    initial_delay=1.0,
    max_delay=10.0,
    multiplier=1.5,
    jitter=0.05,
)


def calculate_delay(attempt: int, config: BackoffConfig | None = None) -> float:
    """Calculate delay for a given attempt number with exponential backoff.

    Args:
        attempt: Attempt number (1-indexed, first attempt = 1)
        config: Backoff configuration (uses BOOTSTRAP_BACKOFF if None)

    Returns:
        Delay in seconds with jitter applied

    Example:
        >>> calculate_delay(1)  # ~2.0s (with small jitter)
        >>> calculate_delay(3)  # ~8.0s (with small jitter)
        >>> calculate_delay(10)  # ~30.0s (capped)
    """
    if config is None:
        config = BOOTSTRAP_BACKOFF

    if attempt < 1:
        attempt = 1

    # Calculate base delay: initial * multiplier^(attempt-1)
    base_delay = config.initial_delay * (config.multiplier ** (attempt - 1))

    # Apply cap
    capped_delay = min(base_delay, config.max_delay)

    # Apply jitter: random variation of +/- jitter%
    if config.jitter > 0:
        jitter_range = capped_delay * config.jitter
        jitter_offset = random.uniform(-jitter_range, jitter_range)  # noqa: S311
        final_delay = capped_delay + jitter_offset
    else:
        final_delay = capped_delay

    # Ensure non-negative
    return max(0.0, final_delay)


def calculate_delay_sequence(
    max_attempts: int,
    config: BackoffConfig | None = None,
) -> list[float]:
    """Calculate delay sequence for a given number of attempts.

    Useful for pre-computing delays or logging expected behavior.

    Args:
        max_attempts: Number of attempts to calculate
        config: Backoff configuration

    Returns:
        List of delays (without jitter, for predictability)
    """
    if config is None:
        config = BOOTSTRAP_BACKOFF

    delays = []
    for attempt in range(1, max_attempts + 1):
        base_delay = config.initial_delay * (config.multiplier ** (attempt - 1))
        delays.append(min(base_delay, config.max_delay))
    return delays


class ExponentialBackoff:
    """Iterator for exponential backoff polling loops.

    Yields (attempt_number, delay) tuples for each iteration.
    Respects max_attempts and total_timeout limits.

    Usage:
        backoff = ExponentialBackoff(max_attempts=10)
        for attempt, delay in backoff:
            result = poll_operation()
            if result.success:
                break
            time.sleep(delay)

        # With timeout
        backoff = ExponentialBackoff(total_timeout=120.0)
        for attempt, delay in backoff:
            if try_connect():
                break
            time.sleep(delay)
    """

    def __init__(
        self,
        config: BackoffConfig | None = None,
        *,
        max_attempts: int | None = None,
        total_timeout: float | None = None,
    ) -> None:
        """Initialize exponential backoff iterator.

        Args:
            config: Backoff configuration (defaults to BOOTSTRAP_BACKOFF)
            max_attempts: Maximum number of iterations (None = unlimited)
            total_timeout: Maximum total elapsed time in seconds (None = unlimited)

        Note:
            If neither max_attempts nor total_timeout is specified,
            the iterator will yield indefinitely. Use with caution.
        """
        self.config = config or BOOTSTRAP_BACKOFF
        self.max_attempts = max_attempts
        self.total_timeout = total_timeout

        self._attempt = 0
        self._elapsed = 0.0
        self._start_time: float | None = None

    def __iter__(self) -> Iterator[tuple[int, float]]:
        """Return iterator."""
        return self

    def __next__(self) -> tuple[int, float]:
        """Yield next (attempt, delay) tuple.

        Returns:
            Tuple of (attempt_number, delay_seconds)

        Raises:
            StopIteration: When max_attempts or total_timeout exceeded
        """
        import time

        # Initialize start time on first iteration
        if self._start_time is None:
            self._start_time = time.monotonic()

        # Check max attempts
        if self.max_attempts is not None and self._attempt >= self.max_attempts:
            raise StopIteration

        # Check total timeout
        if self.total_timeout is not None:
            elapsed = time.monotonic() - self._start_time
            if elapsed >= self.total_timeout:
                raise StopIteration

        self._attempt += 1
        delay = calculate_delay(self._attempt, self.config)

        # Adjust delay if it would exceed timeout
        if self.total_timeout is not None:
            elapsed = time.monotonic() - self._start_time
            remaining = self.total_timeout - elapsed
            if remaining <= 0:
                raise StopIteration
            delay = min(delay, remaining)

        return self._attempt, delay

    @property
    def attempt(self) -> int:
        """Current attempt number."""
        return self._attempt

    def reset(self) -> None:
        """Reset iterator to initial state."""
        self._attempt = 0
        self._elapsed = 0.0
        self._start_time = None

    def elapsed(self) -> float:
        """Return elapsed time since first iteration."""
        import time

        if self._start_time is None:
            return 0.0
        return time.monotonic() - self._start_time


def backoff_with_deadline(
    deadline: float,
    config: BackoffConfig | None = None,
) -> ExponentialBackoff:
    """Create backoff iterator that respects an absolute deadline.

    Args:
        deadline: Absolute time (from time.monotonic()) to stop
        config: Backoff configuration

    Returns:
        ExponentialBackoff configured with remaining time as timeout

    Usage:
        import time
        deadline = time.monotonic() + 120  # 120 seconds from now
        for attempt, delay in backoff_with_deadline(deadline):
            if poll_successful():
                break
            time.sleep(delay)
    """
    import time

    remaining = max(0.0, deadline - time.monotonic())
    return ExponentialBackoff(config, total_timeout=remaining)
