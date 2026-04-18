"""OAuth state management for CSRF protection.

Stores OAuth state tokens in Redis with a short TTL to prevent CSRF attacks
and replay attacks during the OAuth flow.

Uses the centralized Redis client with circuit breaker protection to prevent
retry storms during Redis outages.
"""

import json
import logging
from datetime import timedelta
from typing import Any

from app.logging import get_logger
from app.services.redis_client import get_redis

logger = logging.getLogger(__name__)
logger = get_logger(__name__)

STATE_TTL = timedelta(minutes=10)
STATE_PREFIX = "oauth_state:"


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

    Raises:
        RuntimeError: If Redis is unavailable (circuit breaker open)
    """
    client = get_redis()
    if client is None:
        logger.error(
            "failed_to_store_oauth_state error=Redis unavailable (circuit open)"
        )
        raise RuntimeError("Redis unavailable for OAuth state storage")
    try:
        client.setex(
            f"{STATE_PREFIX}{state}",
            STATE_TTL,
            json.dumps(data),
        )
        logger.debug("stored_oauth_state state=%s...", state[:8])
    except Exception as exc:
        logger.error("failed_to_store_oauth_state error=%s", exc)
        raise


def get_and_delete_oauth_state(state: str) -> dict[str, Any] | None:
    """Get and delete OAuth state (one-time use).

    This atomically retrieves and deletes the state, ensuring it can only
    be used once (preventing replay attacks).

    Args:
        state: The state token from the OAuth callback

    Returns:
        The associated data dict, or None if state not found, expired, or Redis unavailable
    """
    client = get_redis()
    if client is None:
        logger.error("failed_to_get_oauth_state error=Redis unavailable (circuit open)")
        return None
    try:
        key = f"{STATE_PREFIX}{state}"

        # Use pipeline for atomic get-then-delete
        pipe = client.pipeline()
        pipe.get(key)
        pipe.delete(key)
        results = pipe.execute()

        data = results[0]
        if data:
            logger.debug("retrieved_oauth_state state=%s...", state[:8])
            if isinstance(data, str):
                return _loads_dict(data)

        logger.warning("oauth_state_not_found state=%s...", state[:8])
        return None

    except Exception as exc:
        logger.error("failed_to_get_oauth_state error=%s", exc)
        return None


def verify_oauth_state(state: str) -> dict[str, Any] | None:
    """Verify OAuth state exists without deleting it.

    Use this for verification checks before processing the callback.
    Use get_and_delete_oauth_state() for the actual consumption.

    Args:
        state: The state token to verify

    Returns:
        The associated data dict, or None if state not found or Redis unavailable
    """
    client = get_redis()
    if client is None:
        logger.error(
            "failed_to_verify_oauth_state error=Redis unavailable (circuit open)"
        )
        return None
    try:
        key = f"{STATE_PREFIX}{state}"
        data = client.get(key)

        if data:
            return _loads_dict(str(data))
        return None

    except Exception as exc:
        logger.error("failed_to_verify_oauth_state error=%s", exc)
        return None


def delete_oauth_state(state: str) -> bool:
    """Delete an OAuth state token.

    Args:
        state: The state token to delete

    Returns:
        True if deleted, False if not found or Redis unavailable
    """
    client = get_redis()
    if client is None:
        logger.error(
            "failed_to_delete_oauth_state error=Redis unavailable (circuit open)"
        )
        return False
    try:
        key = f"{STATE_PREFIX}{state}"
        deleted: int = client.delete(key)  # type: ignore[assignment]
        return deleted > 0
    except Exception as exc:
        logger.error("failed_to_delete_oauth_state error=%s", exc)
        return False
