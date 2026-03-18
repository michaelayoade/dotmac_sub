"""CRM webhook dispatcher — push subscriber changes to DotMac Omni CRM."""

from __future__ import annotations

import logging
import time
from datetime import UTC, datetime

from requests import RequestException, post

from app.config import settings

logger = logging.getLogger(__name__)

# Token cache
_cached_token: str | None = None
_token_expires_at: float = 0


def _get_token() -> str | None:
    """Get a valid JWT token, refreshing if needed."""
    global _cached_token, _token_expires_at

    if _cached_token and time.time() < _token_expires_at - 60:
        return _cached_token

    try:
        resp = post(
            f"{settings.crm_base_url}/api/v1/auth/login",
            json={"username": settings.crm_username, "password": settings.crm_password},
            timeout=10,
        )
        if resp.status_code == 200:
            data = resp.json()
            _cached_token = data.get("access_token")
            _token_expires_at = time.time() + 840  # 14 min (token lasts 15)
            return _cached_token
        logger.warning("CRM login failed: %d %s", resp.status_code, resp.text[:100])
    except RequestException as e:
        logger.warning("CRM login error: %s", e)
    return None


def push_subscriber_change(
    splynx_customer_id: int,
    subscriber_data: dict,
) -> bool:
    """Push a subscriber change to the CRM webhook endpoint.

    Args:
        splynx_customer_id: The Splynx customer ID (used as external_id).
        subscriber_data: Dict with subscriber fields matching Splynx API format.

    Returns:
        True if successful.
    """
    token = _get_token()
    if not token:
        logger.warning("Cannot push to CRM: no auth token")
        return False

    payload = {"id": splynx_customer_id, **subscriber_data}

    try:
        resp = post(
            f"{settings.crm_base_url}/api/v1/subscribers/sync/webhook/splynx",
            json=payload,
            headers={"Authorization": f"Bearer {token}"},
            timeout=15,
        )
        if resp.status_code == 200:
            logger.debug("CRM webhook OK for customer %d", splynx_customer_id)
            return True
        logger.warning(
            "CRM webhook failed for customer %d: %d %s",
            splynx_customer_id, resp.status_code, resp.text[:200],
        )
    except RequestException as e:
        logger.warning("CRM webhook error for customer %d: %s", splynx_customer_id, e)
    return False


def push_status_change(
    splynx_customer_id: int,
    new_status: str,
    name: str = "",
) -> bool:
    """Push a subscriber status change to CRM."""
    return push_subscriber_change(
        splynx_customer_id,
        {
            "status": new_status,
            "name": name,
            "last_update": datetime.now(UTC).isoformat(),
        },
    )


def push_service_activation(
    splynx_customer_id: int,
    service_name: str,
    service_speed: str = "",
    status: str = "active",
) -> bool:
    """Push a service activation/change to CRM."""
    return push_subscriber_change(
        splynx_customer_id,
        {
            "status": status,
            "service_name": service_name,
            "service_speed": service_speed,
            "last_update": datetime.now(UTC).isoformat(),
        },
    )
