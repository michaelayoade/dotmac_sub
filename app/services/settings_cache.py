"""Redis-based settings cache for domain settings.

This module provides a centralized cache for domain settings using Redis,
eliminating race conditions with in-memory caches in multi-worker environments.
"""

import json
import logging
import os
from typing import Any, cast

import redis

logger = logging.getLogger(__name__)

_redis_client: redis.Redis | None = None


def get_settings_redis() -> redis.Redis:
    """Get Redis client for settings cache.

    Returns a Redis client connected to the configured Redis URL.
    The client is cached globally for reuse.
    """
    global _redis_client
    if _redis_client is None:
        redis_url = os.environ.get("REDIS_URL", "redis://localhost:6379/0")
        _redis_client = redis.Redis.from_url(
            redis_url,
            decode_responses=True,
        )
    return _redis_client


class SettingsCache:
    """Redis-based cache for domain settings.

    Provides get/set/invalidate operations for settings values.
    Uses a short TTL to balance performance with consistency.
    """

    PREFIX = "settings:"
    TTL = 30  # seconds

    @staticmethod
    def _cache_key(domain: str, key: str) -> str:
        """Build the Redis cache key."""
        return f"{SettingsCache.PREFIX}{domain}:{key}"

    @staticmethod
    def get(domain: str, key: str) -> Any | None:
        """Get a setting value from cache.

        Args:
            domain: The setting domain (e.g., "billing", "collections")
            key: The setting key

        Returns:
            The cached value, or None if not cached or on error
        """
        try:
            r = get_settings_redis()
            cache_key = SettingsCache._cache_key(domain, key)
            value = cast(str | None, r.get(cache_key))
            if value is not None:
                return json.loads(value)
        except redis.RedisError as exc:
            logger.warning(f"Settings cache get failed: {exc}")
        except json.JSONDecodeError as exc:
            logger.warning(f"Settings cache JSON decode failed: {exc}")
        return None

    @staticmethod
    def set(domain: str, key: str, value: Any) -> bool:
        """Set a setting value in cache.

        Args:
            domain: The setting domain
            key: The setting key
            value: The value to cache (must be JSON serializable)

        Returns:
            True if cached successfully, False on error
        """
        try:
            r = get_settings_redis()
            cache_key = SettingsCache._cache_key(domain, key)
            r.setex(cache_key, SettingsCache.TTL, json.dumps(value))
            return True
        except redis.RedisError as exc:
            logger.warning(f"Settings cache set failed: {exc}")
        except (TypeError, ValueError) as exc:
            logger.warning(f"Settings cache JSON encode failed: {exc}")
        return False

    @staticmethod
    def invalidate(domain: str, key: str) -> bool:
        """Invalidate a specific setting in cache.

        Args:
            domain: The setting domain
            key: The setting key

        Returns:
            True if invalidated successfully, False on error
        """
        try:
            r = get_settings_redis()
            cache_key = SettingsCache._cache_key(domain, key)
            r.delete(cache_key)
            return True
        except redis.RedisError as exc:
            logger.warning(f"Settings cache invalidate failed: {exc}")
        return False

    @staticmethod
    def invalidate_domain(domain: str) -> int:
        """Invalidate all settings for a domain.

        Args:
            domain: The setting domain

        Returns:
            Number of keys invalidated, or -1 on error
        """
        try:
            r = get_settings_redis()
            pattern = f"{SettingsCache.PREFIX}{domain}:*"
            count = 0
            for key in r.scan_iter(pattern):
                r.delete(key)
                count += 1
            return count
        except redis.RedisError as exc:
            logger.warning(f"Settings cache invalidate_domain failed: {exc}")
        return -1

    @staticmethod
    def get_multi(domain: str, keys: list[str]) -> dict[str, Any]:
        """Get multiple setting values from cache atomically.

        Args:
            domain: The setting domain
            keys: List of setting keys

        Returns:
            Dict mapping keys to their cached values (missing keys are omitted)
        """
        result = {}
        try:
            r = get_settings_redis()
            cache_keys = [SettingsCache._cache_key(domain, k) for k in keys]
            values = cast(list[str | None], r.mget(cache_keys))
            for key, value in zip(keys, values):
                if value is not None:
                    try:
                        result[key] = json.loads(value)
                    except json.JSONDecodeError:
                        pass
        except redis.RedisError as exc:
            logger.warning(f"Settings cache get_multi failed: {exc}")
        return result

    @staticmethod
    def set_multi(domain: str, values: dict[str, Any]) -> bool:
        """Set multiple setting values in cache atomically.

        Args:
            domain: The setting domain
            values: Dict mapping keys to values

        Returns:
            True if all values cached successfully, False on error
        """
        try:
            r = get_settings_redis()
            pipe = r.pipeline()
            for key, value in values.items():
                cache_key = SettingsCache._cache_key(domain, key)
                pipe.setex(cache_key, SettingsCache.TTL, json.dumps(value))
            pipe.execute()
            return True
        except redis.RedisError as exc:
            logger.warning(f"Settings cache set_multi failed: {exc}")
        except (TypeError, ValueError) as exc:
            logger.warning(f"Settings cache JSON encode failed: {exc}")
        return False
