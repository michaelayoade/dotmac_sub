"""OAuth state management for CSRF protection.

Stores OAuth state tokens in Redis with a short TTL to prevent CSRF attacks
and replay attacks during the OAuth flow.

Environment Variables:
    REDIS_URL: Redis connection URL (default: redis://localhost:6379/0)
"""

import json
import os
from datetime import timedelta
from typing import Any, cast

import redis

from app.logging import get_logger

logger = get_logger(__name__)

REDIS_URL = os.getenv("REDIS_URL", "redis://localhost:6379/0")
STATE_TTL = timedelta(minutes=10)
STATE_PREFIX = "oauth_state:"


def _get_redis_client() -> redis.Redis:
    """Get a Redis client connection."""
    return cast(redis.Redis, redis.from_url(REDIS_URL, decode_responses=True))


def _loads_dict(value: str) -> dict[str, Any] | None:
    """Best-effort JSON->dict loader for state payloads."""
    try:
        parsed = json.loads(value)
    except json.JSONDecodeError:
        return None
    return parsed if isinstance(parsed, dict) else None


def store_oauth_state(state: str, data: dict) -> None:
    """Store OAuth state with associated data.

    The state is used as a CSRF token during the OAuth flow. The associated
    data can include the connector_config_id, redirect URL, etc.

    Args:
        state: The random state token generated for this OAuth flow
        data: Dict of data to associate with this state (e.g., connector_config_id)
    """
    try:
        client = _get_redis_client()
        client.setex(
            f"{STATE_PREFIX}{state}",
            STATE_TTL,
            json.dumps(data),
        )
        logger.debug("stored_oauth_state state=%s...", state[:8])
    except redis.RedisError as exc:
        logger.error("failed_to_store_oauth_state error=%s", exc)
        raise


def get_and_delete_oauth_state(state: str) -> dict[str, Any] | None:
    """Get and delete OAuth state (one-time use).

    This atomically retrieves and deletes the state, ensuring it can only
    be used once (preventing replay attacks).

    Args:
        state: The state token from the OAuth callback

    Returns:
        The associated data dict, or None if state not found or expired
    """
    try:
        client = _get_redis_client()
        key = f"{STATE_PREFIX}{state}"

        # Use pipeline for atomic get-then-delete
        pipe = cast(Any, client.pipeline())
        pipe.get(key)
        pipe.delete(key)
        results = cast(list[object], pipe.execute())

        data = results[0]
        if data:
            logger.debug("retrieved_oauth_state state=%s...", state[:8])
            if isinstance(data, str):
                return _loads_dict(data)

        logger.warning("oauth_state_not_found state=%s...", state[:8])
        return None

    except redis.RedisError as exc:
        logger.error("failed_to_get_oauth_state error=%s", exc)
        return None


def verify_oauth_state(state: str) -> dict[str, Any] | None:
    """Verify OAuth state exists without deleting it.

    Use this for verification checks before processing the callback.
    Use get_and_delete_oauth_state() for the actual consumption.

    Args:
        state: The state token to verify

    Returns:
        The associated data dict, or None if state not found
    """
    try:
        client = _get_redis_client()
        key = f"{STATE_PREFIX}{state}"
        data = cast(Any, client.get(key))

        if data:
            return _loads_dict(str(data))
        return None

    except redis.RedisError as exc:
        logger.error("failed_to_verify_oauth_state error=%s", exc)
        return None


def delete_oauth_state(state: str) -> bool:
    """Delete an OAuth state token.

    Args:
        state: The state token to delete

    Returns:
        True if deleted, False if not found
    """
    try:
        client = _get_redis_client()
        key = f"{STATE_PREFIX}{state}"
        result = cast(int, client.delete(key))
        return result > 0
    except redis.RedisError as exc:
        logger.error("failed_to_delete_oauth_state error=%s", exc)
        return False
