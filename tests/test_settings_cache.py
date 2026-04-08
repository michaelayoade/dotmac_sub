"""Tests for the settings cache with centralized Redis client."""

from __future__ import annotations

import redis

from app.services import redis_client
from app.services import settings_cache


def test_settings_cache_returns_none_when_redis_unavailable(monkeypatch, caplog):
    """When Redis is unavailable, cache operations should return None/False gracefully."""
    # Reset the centralized client state
    redis_client.reset_redis_client()

    class _FailingRedis:
        def ping(self):
            raise redis.AuthenticationError("Authentication required.")

    monkeypatch.setattr(
        redis_client.redis.Redis,
        "from_url",
        lambda *args, **kwargs: _FailingRedis(),
    )

    with caplog.at_level("WARNING"):
        # Operations should fail gracefully
        assert settings_cache.SettingsCache.get("billing", "currency") is None
        assert settings_cache.SettingsCache.set("billing", "currency", "NGN") is False
        assert settings_cache.SettingsCache.invalidate("billing", "currency") is False

    # Circuit breaker should have recorded failures
    state = redis_client.get_circuit_state()
    assert state["failure_count"] >= 1

    # Reset for other tests
    redis_client.reset_redis_client()


def test_settings_cache_uses_client_when_redis_is_available(monkeypatch):
    """When Redis is available, cache operations should work correctly."""
    # Reset the centralized client state
    redis_client.reset_redis_client()

    class _RedisStub:
        def __init__(self):
            self.values: dict[str, str] = {}

        def ping(self):
            return True

        def get(self, key):
            return self.values.get(key)

        def setex(self, key, ttl, value):
            self.values[key] = value

        def delete(self, key):
            self.values.pop(key, None)

        def info(self):
            return {"redis_version": "7.0.0"}

    stub = _RedisStub()
    monkeypatch.setattr(
        redis_client.redis.Redis,
        "from_url",
        lambda *args, **kwargs: stub,
    )

    # Operations should succeed
    assert settings_cache.SettingsCache.set("billing", "currency", "NGN") is True
    assert settings_cache.SettingsCache.get("billing", "currency") == "NGN"
    assert settings_cache.SettingsCache.invalidate("billing", "currency") is True
    assert settings_cache.SettingsCache.get("billing", "currency") is None

    # Reset for other tests
    redis_client.reset_redis_client()


def test_settings_cache_circuit_breaker_opens_after_failures(monkeypatch, caplog):
    """Circuit breaker should open after repeated failures."""
    # Reset the centralized client state
    redis_client.reset_redis_client()

    class _FailingRedis:
        def ping(self):
            raise redis.ConnectionError("Connection refused")

    monkeypatch.setattr(
        redis_client.redis.Redis,
        "from_url",
        lambda *args, **kwargs: _FailingRedis(),
    )

    # Make enough requests to trigger circuit breaker (3 failures)
    for _ in range(5):
        settings_cache.SettingsCache.get("test", "key")

    # Circuit should be open
    state = redis_client.get_circuit_state()
    assert state["circuit_open"] is True
    assert state["failure_count"] >= 3

    # Reset for other tests
    redis_client.reset_redis_client()


def test_settings_cache_multi_operations(monkeypatch):
    """Test multi-get and multi-set operations."""
    # Reset the centralized client state
    redis_client.reset_redis_client()

    class _RedisStub:
        def __init__(self):
            self.values: dict[str, str] = {}

        def ping(self):
            return True

        def get(self, key):
            return self.values.get(key)

        def setex(self, key, ttl, value):
            self.values[key] = value

        def delete(self, key):
            self.values.pop(key, None)

        def mget(self, keys):
            return [self.values.get(k) for k in keys]

        def scan_iter(self, pattern):
            prefix = pattern.rstrip("*")
            return iter([k for k in self.values.keys() if k.startswith(prefix)])

        def pipeline(self):
            return _RedisPipelineStub(self)

        def info(self):
            return {"redis_version": "7.0.0"}

    class _RedisPipelineStub:
        def __init__(self, parent):
            self._parent = parent
            self._ops: list[tuple] = []

        def setex(self, key, ttl, value):
            self._ops.append(("setex", key, ttl, value))
            return self

        def execute(self):
            for op in self._ops:
                if op[0] == "setex":
                    self._parent.values[op[1]] = op[3]
            return [True] * len(self._ops)

    stub = _RedisStub()
    monkeypatch.setattr(
        redis_client.redis.Redis,
        "from_url",
        lambda *args, **kwargs: stub,
    )

    # Test set_multi
    assert settings_cache.SettingsCache.set_multi(
        "billing", {"currency": '"USD"', "tax_rate": "0.1"}
    ) is True

    # Test get_multi
    result = settings_cache.SettingsCache.get_multi("billing", ["currency", "tax_rate"])
    assert "currency" in result
    assert "tax_rate" in result

    # Test invalidate_domain
    count = settings_cache.SettingsCache.invalidate_domain("billing")
    assert count >= 0

    # Reset for other tests
    redis_client.reset_redis_client()
