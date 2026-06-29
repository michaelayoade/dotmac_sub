"""Shared Redis-first session storage helpers.

Uses the centralized Redis client with circuit breaker protection
to prevent retry storms during Redis outages.
"""

from __future__ import annotations

import json
import logging
import os
from datetime import UTC, datetime
from typing import Any, cast

import redis

from app.services.redis_client import get_redis

logger = logging.getLogger(__name__)


def _fallback_enabled() -> bool:
    # Tests expect sessions to work without Redis.
    if os.getenv("PYTEST_CURRENT_TEST"):
        return True
    # Production-safe default: require explicit opt-in for in-memory fallback.
    value = os.getenv("SESSION_IN_MEMORY_FALLBACK", "false")
    return value.strip().lower() in {"1", "true", "yes", "on"}


def _decode_redis_text(value: object) -> str:
    if isinstance(value, bytes):
        return value.decode()
    return str(value)


def get_session_redis() -> redis.Redis | None:
    """Get a shared Redis client for session storage.

    Uses the centralized Redis client with circuit breaker protection.
    """
    return get_redis()


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
    principal_id: str | None = None,
    fallback_index: dict[str, set[str]] | None = None,
) -> None:
    client = get_session_redis()
    if client:
        try:
            ttl = max(1, int(ttl_seconds))
            client.setex(
                f"{prefix}:{session_token}",
                ttl,
                json.dumps(payload),
            )
            if principal_id:
                index_key = _principal_sessions_key(prefix, str(principal_id))
                client.sadd(index_key, session_token)
                index_ttl = client.ttl(index_key)
                if index_ttl < ttl:
                    client.expire(index_key, ttl)
            fallback_store.pop(session_token, None)
            if principal_id and fallback_index is not None:
                fallback_index.get(str(principal_id), set()).discard(session_token)
            return
        except (redis.RedisError, TypeError, ValueError) as exc:
            logger.warning("Session write failed, falling back to memory: %s", exc)
    if _fallback_enabled():
        fallback_store[session_token] = payload
        if principal_id and fallback_index is not None:
            fallback_index.setdefault(str(principal_id), set()).add(session_token)
        return
    raise RuntimeError("Session store unavailable and in-memory fallback is disabled")


def delete_session(
    prefix: str,
    session_token: str,
    fallback_store: dict[str, dict[str, Any]],
    principal_id: str | None = None,
    fallback_index: dict[str, set[str]] | None = None,
) -> None:
    client = get_session_redis()
    if client:
        try:
            client.delete(f"{prefix}:{session_token}")
            if principal_id:
                client.srem(_principal_sessions_key(prefix, str(principal_id)), session_token)
        except redis.RedisError as exc:
            logger.warning("Session delete failed in Redis: %s", exc)
    if _fallback_enabled():
        fallback_store.pop(session_token, None)
        if principal_id and fallback_index is not None:
            tokens = fallback_index.get(str(principal_id))
            if tokens is not None:
                tokens.discard(session_token)
                if not tokens:
                    fallback_index.pop(str(principal_id), None)


def _principal_sessions_key(prefix: str, principal_id: str) -> str:
    return f"{prefix}:principal:{principal_id}"


def list_sessions_for_principal(
    prefix: str,
    principal_id: str,
    fallback_store: dict[str, dict[str, Any]],
    fallback_index: dict[str, set[str]],
) -> list[tuple[str, dict[str, Any]]]:
    """Return active session payloads tracked for a principal."""
    principal = str(principal_id)
    sessions: list[tuple[str, dict[str, Any]]] = []
    client = get_session_redis()
    if client:
        try:
            raw_tokens = client.smembers(_principal_sessions_key(prefix, principal))
            for raw_token in raw_tokens:
                token = _decode_redis_text(raw_token)
                payload = load_session(prefix, token, fallback_store)
                if payload is None:
                    client.srem(_principal_sessions_key(prefix, principal), token)
                    continue
                sessions.append((token, payload))
            return sessions
        except redis.RedisError as exc:
            logger.warning("Session index read failed, falling back to memory: %s", exc)
    if not _fallback_enabled():
        return sessions
    indexed_tokens = set(fallback_index.get(principal, set()))
    for token in indexed_tokens:
        payload = fallback_store.get(token)
        if payload is None:
            fallback_index.get(principal, set()).discard(token)
            continue
        sessions.append((token, payload))
    return sessions


def _epoch_key(prefix: str, principal_id: str) -> str:
    return f"{prefix}:epoch:{principal_id}"


def set_session_revocation_epoch(
    prefix: str,
    principal_id: str,
    ttl_seconds: int,
    fallback_epochs: dict[str, str],
) -> str:
    """Mark every session for the principal created before now as revoked.

    Portal sessions are opaque Redis keys with no per-principal index, so
    revoke-all works by stamping an epoch; ``get_session``-side checks compare
    the session's ``created_at`` against it. The epoch TTL must cover the
    longest session lifetime so a pre-revocation session can't outlive it.
    """
    now_iso = datetime.now(UTC).isoformat()
    client = get_session_redis()
    if client:
        try:
            client.setex(
                _epoch_key(prefix, principal_id), max(1, int(ttl_seconds)), now_iso
            )
        except redis.RedisError as exc:
            logger.warning("Session revocation epoch write failed: %s", exc)
    if _fallback_enabled():
        fallback_epochs[str(principal_id)] = now_iso
    return now_iso


def get_session_revocation_epoch(
    prefix: str,
    principal_id: str,
    fallback_epochs: dict[str, str],
) -> str | None:
    client = get_session_redis()
    if client:
        try:
            raw = client.get(_epoch_key(prefix, principal_id))
            if raw:
                return str(cast(str, raw))
        except redis.RedisError as exc:
            logger.warning("Session revocation epoch read failed: %s", exc)
    if _fallback_enabled():
        return fallback_epochs.get(str(principal_id))
    return None
