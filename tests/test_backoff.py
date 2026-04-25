"""Tests for exponential backoff utilities."""

import time

import pytest

from app.services.network.backoff import (
    BOOTSTRAP_BACKOFF,
    FAST_POLL_BACKOFF,
    TASK_RETRY_BACKOFF,
    BackoffConfig,
    ExponentialBackoff,
    backoff_with_deadline,
    calculate_delay,
    calculate_delay_sequence,
)


class TestBackoffConfig:
    """Tests for BackoffConfig dataclass."""

    def test_default_values(self):
        """Default config should have sensible values."""
        config = BackoffConfig()
        assert config.initial_delay == 2.0
        assert config.max_delay == 30.0
        assert config.multiplier == 2.0
        assert config.jitter == 0.1

    def test_custom_values(self):
        """Custom values should be set correctly."""
        config = BackoffConfig(
            initial_delay=5.0,
            max_delay=60.0,
            multiplier=1.5,
            jitter=0.2,
        )
        assert config.initial_delay == 5.0
        assert config.max_delay == 60.0
        assert config.multiplier == 1.5
        assert config.jitter == 0.2

    def test_frozen(self):
        """Config should be immutable."""
        config = BackoffConfig()
        with pytest.raises(Exception):  # FrozenInstanceError
            config.initial_delay = 10.0

    def test_invalid_initial_delay(self):
        """Initial delay must be positive."""
        with pytest.raises(ValueError, match="initial_delay must be positive"):
            BackoffConfig(initial_delay=0)

        with pytest.raises(ValueError, match="initial_delay must be positive"):
            BackoffConfig(initial_delay=-1)

    def test_invalid_max_delay(self):
        """Max delay must be >= initial delay."""
        with pytest.raises(ValueError, match="max_delay must be >= initial_delay"):
            BackoffConfig(initial_delay=10.0, max_delay=5.0)

    def test_invalid_multiplier(self):
        """Multiplier must be >= 1.0."""
        with pytest.raises(ValueError, match="multiplier must be >= 1.0"):
            BackoffConfig(multiplier=0.5)

    def test_invalid_jitter(self):
        """Jitter must be in [0, 1)."""
        with pytest.raises(ValueError, match="jitter must be in"):
            BackoffConfig(jitter=-0.1)

        with pytest.raises(ValueError, match="jitter must be in"):
            BackoffConfig(jitter=1.0)


class TestPredefinedConfigs:
    """Tests for predefined backoff configurations."""

    def test_bootstrap_backoff(self):
        """BOOTSTRAP_BACKOFF should be optimized for TR-069 polling."""
        assert BOOTSTRAP_BACKOFF.initial_delay == 2.0
        assert BOOTSTRAP_BACKOFF.max_delay == 30.0
        assert BOOTSTRAP_BACKOFF.multiplier == 2.0

    def test_task_retry_backoff(self):
        """TASK_RETRY_BACKOFF should be optimized for task retries."""
        assert TASK_RETRY_BACKOFF.initial_delay == 30.0
        assert TASK_RETRY_BACKOFF.max_delay == 240.0
        assert TASK_RETRY_BACKOFF.multiplier == 2.0

    def test_fast_poll_backoff(self):
        """FAST_POLL_BACKOFF should be optimized for fast polling."""
        assert FAST_POLL_BACKOFF.initial_delay == 1.0
        assert FAST_POLL_BACKOFF.max_delay == 10.0
        assert FAST_POLL_BACKOFF.multiplier == 1.5


class TestCalculateDelay:
    """Tests for calculate_delay function."""

    def test_first_attempt(self):
        """First attempt should return initial delay."""
        config = BackoffConfig(initial_delay=2.0, jitter=0)
        delay = calculate_delay(1, config)
        assert delay == 2.0

    def test_exponential_growth(self):
        """Delay should grow exponentially."""
        config = BackoffConfig(initial_delay=2.0, multiplier=2.0, jitter=0)

        assert calculate_delay(1, config) == 2.0
        assert calculate_delay(2, config) == 4.0
        assert calculate_delay(3, config) == 8.0
        assert calculate_delay(4, config) == 16.0

    def test_cap_at_max_delay(self):
        """Delay should cap at max_delay."""
        config = BackoffConfig(
            initial_delay=2.0, max_delay=10.0, multiplier=2.0, jitter=0
        )

        assert calculate_delay(1, config) == 2.0
        assert calculate_delay(2, config) == 4.0
        assert calculate_delay(3, config) == 8.0
        assert calculate_delay(4, config) == 10.0  # Capped
        assert calculate_delay(5, config) == 10.0  # Still capped
        assert calculate_delay(100, config) == 10.0  # Still capped

    def test_jitter_applied(self):
        """Jitter should add variation."""
        config = BackoffConfig(initial_delay=10.0, jitter=0.1, max_delay=100.0)

        delays = [calculate_delay(1, config) for _ in range(100)]
        min_delay = min(delays)
        max_delay = max(delays)

        # With 10% jitter on 10.0, we expect [9.0, 11.0] range
        assert min_delay >= 9.0
        assert max_delay <= 11.0
        # There should be some variation
        assert min_delay != max_delay

    def test_zero_jitter(self):
        """Zero jitter should give consistent results."""
        config = BackoffConfig(initial_delay=5.0, jitter=0)

        delays = [calculate_delay(1, config) for _ in range(10)]
        assert all(d == 5.0 for d in delays)

    def test_uses_default_config(self):
        """Should use BOOTSTRAP_BACKOFF if no config provided."""
        delay = calculate_delay(1)
        # Default is 2.0 with 10% jitter, so should be near 2.0
        assert 1.8 <= delay <= 2.2

    def test_invalid_attempt_normalized(self):
        """Attempt < 1 should be treated as 1."""
        config = BackoffConfig(initial_delay=5.0, jitter=0)

        assert calculate_delay(0, config) == 5.0
        assert calculate_delay(-5, config) == 5.0


class TestCalculateDelaySequence:
    """Tests for calculate_delay_sequence function."""

    def test_basic_sequence(self):
        """Generate correct sequence of delays."""
        config = BackoffConfig(initial_delay=2.0, multiplier=2.0, jitter=0)
        sequence = calculate_delay_sequence(5, config)

        assert sequence == [2.0, 4.0, 8.0, 16.0, 30.0]  # 32 capped to 30

    def test_sequence_with_custom_cap(self):
        """Sequence should respect max_delay."""
        config = BackoffConfig(
            initial_delay=1.0, max_delay=5.0, multiplier=2.0, jitter=0
        )
        sequence = calculate_delay_sequence(6, config)

        assert sequence == [1.0, 2.0, 4.0, 5.0, 5.0, 5.0]

    def test_empty_sequence(self):
        """Empty sequence for max_attempts=0."""
        sequence = calculate_delay_sequence(0)
        assert sequence == []


class TestExponentialBackoff:
    """Tests for ExponentialBackoff iterator."""

    def test_basic_iteration(self):
        """Should yield (attempt, delay) tuples."""
        config = BackoffConfig(initial_delay=1.0, jitter=0)
        backoff = ExponentialBackoff(config, max_attempts=3)

        attempts = list(backoff)
        assert len(attempts) == 3
        assert attempts[0][0] == 1  # First attempt
        assert attempts[1][0] == 2
        assert attempts[2][0] == 3

    def test_max_attempts_limit(self):
        """Should stop after max_attempts."""
        backoff = ExponentialBackoff(max_attempts=5)

        count = 0
        for _ in backoff:
            count += 1
        assert count == 5

    def test_attempt_property(self):
        """Should track current attempt number."""
        backoff = ExponentialBackoff(max_attempts=3)

        assert backoff.attempt == 0

        next(backoff)
        assert backoff.attempt == 1

        next(backoff)
        assert backoff.attempt == 2

    def test_reset(self):
        """Reset should clear state."""
        backoff = ExponentialBackoff(max_attempts=5)

        # Consume some iterations
        next(backoff)
        next(backoff)
        assert backoff.attempt == 2

        # Reset
        backoff.reset()
        assert backoff.attempt == 0

        # Should iterate again
        next(backoff)
        assert backoff.attempt == 1

    def test_total_timeout(self):
        """Should stop after total_timeout."""
        # Use a very short timeout
        backoff = ExponentialBackoff(total_timeout=0.1)

        count = 0
        for _, delay in backoff:
            count += 1
            if count > 10:  # Safety limit
                break
            time.sleep(0.05)

        # Should stop after timeout (typically 2-3 iterations with short delays)
        assert count <= 10

    def test_elapsed_time(self):
        """Should track elapsed time."""
        backoff = ExponentialBackoff(max_attempts=3)

        assert backoff.elapsed() == 0.0

        next(backoff)
        time.sleep(0.05)

        elapsed = backoff.elapsed()
        assert elapsed >= 0.05

    def test_uses_default_config(self):
        """Should use BOOTSTRAP_BACKOFF by default."""
        backoff = ExponentialBackoff(max_attempts=1)
        _, delay = next(backoff)

        # Default initial is 2.0 with 10% jitter
        assert 1.8 <= delay <= 2.2

    def test_delay_values_grow(self):
        """Delays should grow exponentially."""
        config = BackoffConfig(initial_delay=1.0, multiplier=2.0, jitter=0)
        backoff = ExponentialBackoff(config, max_attempts=4)

        delays = [delay for _, delay in backoff]
        assert delays == [1.0, 2.0, 4.0, 8.0]


class TestBackoffWithDeadline:
    """Tests for backoff_with_deadline function."""

    def test_respects_deadline(self):
        """Should stop at deadline."""
        deadline = time.monotonic() + 0.2
        backoff = backoff_with_deadline(deadline)

        count = 0
        for _, delay in backoff:
            count += 1
            if count > 20:  # Safety limit
                break
            time.sleep(0.05)

        # Should have stopped near the deadline
        assert time.monotonic() >= deadline - 0.1

    def test_past_deadline(self):
        """Past deadline should yield no iterations."""
        deadline = time.monotonic() - 1.0  # Already passed
        backoff = backoff_with_deadline(deadline)

        count = sum(1 for _ in backoff)
        assert count == 0

    def test_custom_config(self):
        """Should use custom config."""
        config = BackoffConfig(initial_delay=0.5, jitter=0)
        deadline = time.monotonic() + 1.0
        backoff = backoff_with_deadline(deadline, config)

        _, delay = next(backoff)
        assert delay == 0.5


class TestBackoffIntegration:
    """Integration tests for backoff in realistic scenarios."""

    def test_bootstrap_polling_pattern(self):
        """Simulate TR-069 bootstrap polling."""
        config = BackoffConfig(initial_delay=0.01, max_delay=0.05, jitter=0)
        backoff = ExponentialBackoff(config, max_attempts=5)

        found = False
        for attempt, delay in backoff:
            # Simulate polling
            if attempt >= 3:
                found = True
                break
            time.sleep(delay)

        assert found
        assert backoff.attempt == 3

    def test_timeout_scenario(self):
        """Simulate timeout scenario with backoff."""
        config = BackoffConfig(initial_delay=0.01, max_delay=0.1, jitter=0)
        backoff = ExponentialBackoff(config, total_timeout=0.15)

        attempts_made = 0
        for _ in backoff:
            attempts_made += 1
            time.sleep(0.05)

        # Should have made a few attempts before timeout
        assert 1 <= attempts_made <= 5
