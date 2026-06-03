from __future__ import annotations

import json
import logging
import os
from threading import Lock
from typing import Any, cast
from urllib.parse import urlsplit, urlunsplit

import redis
from redis.exceptions import RedisError

from app.services.redis_metrics import timed_operation

logger = logging.getLogger(__name__)

_CACHE_DB_DEFAULT = 3
_CACHE_NAMESPACE_DEFAULT = "appcache:v1"
_cache_client: redis.Redis | None = None
_cache_lock = Lock()


def _cache_db() -> int:
    raw = os.getenv("REDIS_CACHE_DB", str(_CACHE_DB_DEFAULT))
    try:
        return max(0, int(raw))
    except ValueError:
        return _CACHE_DB_DEFAULT


def _cache_namespace() -> str:
    value = str(os.getenv("REDIS_CACHE_NAMESPACE", _CACHE_NAMESPACE_DEFAULT)).strip()
    return value or _CACHE_NAMESPACE_DEFAULT


def _cache_url() -> str:
    base = os.getenv("REDIS_URL", "redis://localhost:6379/0")
    parts = urlsplit(base)
    return urlunsplit(
        (
            parts.scheme,
            parts.netloc,
            f"/{_cache_db()}",
            parts.query,
            parts.fragment,
        )
    )


def cache_key(*parts: object) -> str:
    values = [_cache_namespace()]
    values.extend(str(part).strip(":") for part in parts if str(part))
    return ":".join(values)


def get_cache_redis(force_reconnect: bool = False) -> redis.Redis | None:
    global _cache_client

    with _cache_lock:
        if _cache_client is not None and not force_reconnect:
            return _cache_client

        try:
            client = redis.Redis.from_url(
                _cache_url(),
                decode_responses=True,
                socket_connect_timeout=3,
                socket_timeout=3,
            )
            with timed_operation("app_cache_connect"):
                client.ping()
            _cache_client = client
            return client
        except RedisError as exc:
            logger.warning("app_cache_connect_failed: %s", exc)
            _cache_client = None
            return None


def get_json(key: str) -> Any | None:
    client = get_cache_redis()
    if client is None:
        return None
    try:
        with timed_operation("app_cache_get"):
            raw = cast(str | None, client.get(key))
        if not raw:
            return None
        return json.loads(raw)
    except (RedisError, json.JSONDecodeError) as exc:
        logger.debug("app_cache_get_failed key=%s error=%s", key, exc)
        return None


def get_many_json(keys: list[str]) -> dict[str, Any]:
    if not keys:
        return {}
    client = get_cache_redis()
    if client is None:
        return {}
    try:
        with timed_operation("app_cache_mget"):
            raw_values = cast(list[str | None], client.mget(keys))
    except RedisError as exc:
        logger.debug("app_cache_mget_failed keys=%s error=%s", len(keys), exc)
        return {}

    parsed: dict[str, Any] = {}
    for key, raw in zip(keys, raw_values):
        if not raw:
            continue
        try:
            parsed[key] = json.loads(raw)
        except json.JSONDecodeError:
            logger.debug("app_cache_decode_failed key=%s", key)
    return parsed


def set_json(key: str, value: Any, ttl_seconds: int) -> bool:
    client = get_cache_redis()
    if client is None:
        return False
    try:
        encoded = json.dumps(value, default=str)
        ttl = max(1, int(ttl_seconds))
        with timed_operation("app_cache_set"):
            client.setex(key, ttl, encoded)
        return True
    except (RedisError, TypeError, ValueError) as exc:
        logger.debug("app_cache_set_failed key=%s error=%s", key, exc)
        return False


def delete_key(key: str) -> bool:
    client = get_cache_redis()
    if client is None:
        return False
    try:
        with timed_operation("app_cache_delete"):
            client.delete(key)
        return True
    except RedisError as exc:
        logger.debug("app_cache_delete_failed key=%s error=%s", key, exc)
        return False


def delete_many(keys: list[str]) -> int:
    if not keys:
        return 0
    client = get_cache_redis()
    if client is None:
        return 0
    try:
        with timed_operation("app_cache_delete_many"):
            deleted = cast(int, client.delete(*keys))
        return deleted
    except RedisError as exc:
        logger.debug("app_cache_delete_many_failed keys=%s error=%s", len(keys), exc)
        return 0


def sadd(key: str, value: str, ttl_seconds: int | None = None) -> bool:
    client = get_cache_redis()
    if client is None:
        return False
    try:
        with timed_operation("app_cache_sadd"):
            client.sadd(key, value)
            if ttl_seconds:
                client.expire(key, max(1, int(ttl_seconds)))
        return True
    except RedisError as exc:
        logger.debug("app_cache_sadd_failed key=%s error=%s", key, exc)
        return False


def srem(key: str, value: str) -> bool:
    client = get_cache_redis()
    if client is None:
        return False
    try:
        with timed_operation("app_cache_srem"):
            client.srem(key, value)
        return True
    except RedisError as exc:
        logger.debug("app_cache_srem_failed key=%s error=%s", key, exc)
        return False


def smembers(key: str) -> set[str]:
    client = get_cache_redis()
    if client is None:
        return set()
    try:
        with timed_operation("app_cache_smembers"):
            values = cast(set[object], client.smembers(key))
        return {str(value) for value in values}
    except RedisError as exc:
        logger.debug("app_cache_smembers_failed key=%s error=%s", key, exc)
        return set()


def scan_delete(prefix_key: str) -> int:
    client = get_cache_redis()
    if client is None:
        return 0
    deleted = 0
    try:
        for key in client.scan_iter(match=f"{prefix_key}*"):
            removed = cast(int, client.delete(key))
            deleted += removed
        return deleted
    except RedisError as exc:
        logger.debug("app_cache_scan_delete_failed prefix=%s error=%s", prefix_key, exc)
        return deleted
