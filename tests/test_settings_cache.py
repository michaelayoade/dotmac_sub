from __future__ import annotations

import os

import redis

from app.services import settings_cache


def test_settings_cache_disables_after_redis_auth_failure(monkeypatch, caplog):
    settings_cache._redis_client = None
    settings_cache._cache_disabled = False

    calls = {"count": 0}

    class _FailingRedis:
        def ping(self):
            calls["count"] += 1
            raise redis.AuthenticationError("Authentication required.")

    monkeypatch.setattr(
        settings_cache.redis.Redis,
        "from_url",
        lambda *args, **kwargs: _FailingRedis(),
    )

    with caplog.at_level("WARNING"):
        assert settings_cache.SettingsCache.get("billing", "currency") is None
        assert settings_cache.SettingsCache.set("billing", "currency", "NGN") is False
        assert settings_cache.SettingsCache.invalidate("billing", "currency") is False

    assert calls["count"] == 1
    assert settings_cache._cache_disabled is True
    assert caplog.text.count("Settings cache disabled") == 1


def test_settings_cache_uses_client_when_redis_is_available(monkeypatch):
    settings_cache._redis_client = None
    settings_cache._cache_disabled = False

    class _RedisStub:
        def __init__(self):
            self.values = {}

        def ping(self):
            return True

        def get(self, key):
            return self.values.get(key)

        def setex(self, key, ttl, value):
            self.values[key] = value

        def delete(self, key):
            self.values.pop(key, None)

    stub = _RedisStub()
    monkeypatch.setattr(
        settings_cache.redis.Redis,
        "from_url",
        lambda *args, **kwargs: stub,
    )

    assert settings_cache.SettingsCache.set("billing", "currency", "NGN") is True
    assert settings_cache.SettingsCache.get("billing", "currency") == "NGN"
    assert settings_cache.SettingsCache.invalidate("billing", "currency") is True
    assert settings_cache.SettingsCache.get("billing", "currency") is None


def test_settings_cache_loads_dotenv_before_resolving_redis_url(monkeypatch):
    settings_cache._redis_client = None
    settings_cache._cache_disabled = False
    monkeypatch.delenv("REDIS_URL", raising=False)

    seen: dict[str, str] = {}

    class _RedisStub:
        def ping(self):
            return True

    def _load_dotenv() -> None:
        os.environ["REDIS_URL"] = "redis://:from-dotenv@redis:6379/0"

    def _from_url(url, **kwargs):
        seen["url"] = url
        return _RedisStub()

    monkeypatch.setattr(settings_cache, "load_dotenv", _load_dotenv)
    monkeypatch.setattr(settings_cache.redis.Redis, "from_url", _from_url)

    client = settings_cache.get_settings_redis()

    assert client is not None
    assert seen["url"] == "redis://:from-dotenv@redis:6379/0"
