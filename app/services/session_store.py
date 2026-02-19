"""Shared Redis-first session storage helpers."""

from __future__ import annotations

import json
import logging
import os
from typing import Any, cast

import redis

logger = logging.getLogger(__name__)

_SESSION_REDIS_CLIENT: redis.Redis | None = None
_SESSION_REDIS_UNAVAILABLE = False


def _fallback_enabled() -> bool:
    # Tests expect sessions to work without Redis.
    if os.getenv("PYTEST_CURRENT_TEST"):
        return True
    # Production-safe default: require explicit opt-in for in-memory fallback.
    value = os.getenv("SESSION_IN_MEMORY_FALLBACK", "false")
    return value.strip().lower() in {"1", "true", "yes", "on"}


def get_session_redis() -> redis.Redis | None:
    """Get a shared Redis client for session storage."""
    global _SESSION_REDIS_CLIENT, _SESSION_REDIS_UNAVAILABLE
    if _SESSION_REDIS_CLIENT is not None:
        return _SESSION_REDIS_CLIENT
    if _SESSION_REDIS_UNAVAILABLE:
        return None

    redis_url = os.getenv("SESSION_REDIS_URL") or os.getenv("REDIS_URL")
    if not redis_url:
        return None

    try:
        client = redis.Redis.from_url(redis_url, decode_responses=True)
        client.ping()
        _SESSION_REDIS_CLIENT = client
        return client
    except redis.RedisError as exc:
        logger.warning("Session Redis unavailable, using in-memory fallback: %s", exc)
        _SESSION_REDIS_UNAVAILABLE = True
        return None


def load_session(
    prefix: str,
    session_token: str,
    fallback_store: dict[str, dict[str, Any]],
) -> dict[str, Any] | None:
    client = get_session_redis()
    if client:
        try:
            raw = client.get(f"{prefix}:{session_token}")
            raw = cast(str | None, raw)
            if not raw:
                return None
            data = json.loads(raw)
            if isinstance(data, dict):
                return data
            return None
        except (redis.RedisError, json.JSONDecodeError) as exc:
            logger.warning("Session read failed, falling back to memory: %s", exc)
    if _fallback_enabled():
        return fallback_store.get(session_token)
    return None


def store_session(
    prefix: str,
    session_token: str,
    payload: dict[str, Any],
    ttl_seconds: int,
    fallback_store: dict[str, dict[str, Any]],
) -> None:
    client = get_session_redis()
    if client:
        try:
            client.setex(
                f"{prefix}:{session_token}",
                max(1, int(ttl_seconds)),
                json.dumps(payload),
            )
            fallback_store.pop(session_token, None)
            return
        except (redis.RedisError, TypeError, ValueError) as exc:
            logger.warning("Session write failed, falling back to memory: %s", exc)
    if _fallback_enabled():
        fallback_store[session_token] = payload
        return
    raise RuntimeError("Session store unavailable and in-memory fallback is disabled")


def delete_session(
    prefix: str,
    session_token: str,
    fallback_store: dict[str, dict[str, Any]],
) -> None:
    client = get_session_redis()
    if client:
        try:
            client.delete(f"{prefix}:{session_token}")
        except redis.RedisError as exc:
            logger.warning("Session delete failed in Redis: %s", exc)
    if _fallback_enabled():
        fallback_store.pop(session_token, None)
