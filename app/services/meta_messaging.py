"""Meta messaging service for sending Facebook/Instagram messages.

Handles outbound messaging via Facebook Messenger and Instagram DMs.
"""

import asyncio
import httpx
from datetime import datetime, timezone

from sqlalchemy.orm import Session

from app.logging import get_logger
from app.config import settings
from app.services.settings_spec import resolve_value
from app.models.domain_settings import SettingDomain
from app.models.connector import ConnectorConfig, ConnectorType
from app.models.crm.enums import ChannelType
from app.models.integration import IntegrationTarget, IntegrationTargetType
from app.models.oauth_token import OAuthToken
from app.services.common import coerce_uuid

logger = get_logger(__name__)

_FACEBOOK_REQUIRED_SCOPES = {"pages_messaging"}
_INSTAGRAM_REQUIRED_SCOPES = {"instagram_manage_messages"}


def _ensure_token_scopes(token: OAuthToken | None, required_scopes: set[str], context: str) -> None:
    if not token or not token.scopes:
        return
    if not isinstance(token.scopes, (list, tuple, set)):
        return
    granted = {str(scope) for scope in token.scopes}
    missing = required_scopes - granted
    if missing:
        raise ValueError(f"Missing required Meta permissions for {context}: {sorted(missing)}")


async def _post_with_retry(
    client: httpx.AsyncClient,
    url: str,
    *,
    params: dict | None = None,
    json: dict | None = None,
    timeout: int | float | None = None,
    max_retries: int = 1,
) -> httpx.Response:
    retries = 0
    while True:
        response = await client.post(url, params=params, json=json, timeout=timeout)
        if response.status_code in {429} or response.status_code >= 500:
            if retries >= max_retries:
                return response
            retry_after = response.headers.get("Retry-After")
            delay = 1.0
            if retry_after:
                try:
                    delay = max(0.0, float(retry_after))
                except ValueError:
                    delay = 1.0
            await asyncio.sleep(delay)
            retries += 1
            continue
        return response

def _get_meta_graph_base_url(db: Session) -> str:
    version = resolve_value(db, SettingDomain.comms, "meta_graph_api_version")
    if not version:
        version = settings.meta_graph_api_version
    return f"https://graph.facebook.com/{version}"


def _get_meta_access_token_override(db: Session) -> str | None:
    token = resolve_value(db, SettingDomain.comms, "meta_access_token_override")
    if not token:
        return None
    return token.strip() or None


def _get_token_for_channel(
    db: Session,
    channel_type: ChannelType,
    target: IntegrationTarget | None,
    account_id: str | None = None,
) -> OAuthToken | None:
    """Get appropriate OAuth token for the channel type.

    Args:
        db: Database session
        channel_type: ChannelType.facebook_messenger or ChannelType.instagram_dm
        target: IntegrationTarget to get connector from
        account_id: Optional Meta Page/Instagram account ID to route from

    Returns:
        OAuthToken or None if not found
    """
    if not target or not target.connector_config_id:
        return None

    if channel_type == ChannelType.facebook_messenger:
        account_type = "page"
    elif channel_type == ChannelType.instagram_dm:
        account_type = "instagram_business"
    else:
        return None

    base_query = (
        db.query(OAuthToken)
        .filter(OAuthToken.connector_config_id == target.connector_config_id)
        .filter(OAuthToken.provider == "meta")
        .filter(OAuthToken.account_type == account_type)
        .filter(OAuthToken.is_active.is_(True))
        .filter(OAuthToken.access_token.isnot(None))
    )

    if account_id:
        token = (
            base_query.filter(OAuthToken.external_account_id == account_id)
            .order_by(OAuthToken.created_at.desc())
            .first()
        )
        if token:
            return token

    return base_query.order_by(OAuthToken.created_at.desc()).first()


def _get_any_page_token(
    db: Session,
    target: IntegrationTarget | None,
) -> OAuthToken | None:
    """Get any active page token for the connector.

    Used when we need a token but don't have a specific account in mind.

    Args:
        db: Database session
        target: IntegrationTarget to get connector from

    Returns:
        First active OAuthToken or None
    """
    if not target or not target.connector_config_id:
        return None

    return (
        db.query(OAuthToken)
        .filter(OAuthToken.connector_config_id == target.connector_config_id)
        .filter(OAuthToken.provider == "meta")
        .filter(OAuthToken.account_type == "page")
        .filter(OAuthToken.is_active.is_(True))
        .filter(OAuthToken.access_token.isnot(None))
        .first()
    )


async def send_facebook_message(
    db: Session,
    recipient_psid: str,
    message_text: str,
    target: IntegrationTarget | None = None,
    account_id: str | None = None,
) -> dict:
    """Send a message via Facebook Messenger.

    Args:
        db: Database session
        recipient_psid: Recipient's Page-Scoped ID
        message_text: Message text to send
        target: Optional IntegrationTarget (auto-resolved if not provided)
        account_id: Optional Facebook Page ID to send from

    Returns:
        Dict with 'message_id' and 'recipient_id' from Meta API

    Raises:
        ValueError: If no active token found or token expired
        httpx.HTTPStatusError: If API request fails
    """
    override_token = _get_meta_access_token_override(db)
    token = _get_token_for_channel(
        db, ChannelType.facebook_messenger, target, account_id=account_id
    )
    _ensure_token_scopes(token, _FACEBOOK_REQUIRED_SCOPES, "facebook_messenger")
    if not token and not override_token:
        raise ValueError("No active Facebook Page token found")

    if token and not override_token and token.is_token_expired():
        raise ValueError(
            f"Facebook Page token has expired. Please reconnect. "
            f"(Page: {token.external_account_name})"
        )

    page_id = account_id or (token.external_account_id if token else None)
    if not page_id:
        raise ValueError("No Facebook Page ID available for message send")
    if override_token and not token:
        raise ValueError("No linked Facebook Page token found for override send")

    payload = {
        "recipient": {"id": recipient_psid},
        "messaging_type": "RESPONSE",
        "message": {"text": message_text},
    }

    base_url = _get_meta_graph_base_url(db)
    access_token = override_token or (token.access_token if token else None)
    if not access_token:
        raise ValueError("No access token available for Facebook message send")
    async with httpx.AsyncClient() as client:
        response = await _post_with_retry(
            client,
            f"{base_url.rstrip('/')}/{page_id}/messages",
            params={"access_token": access_token},
            json=payload,
            timeout=30,
        )
        if response.status_code >= 400:
            logger.error(
                "facebook_message_send_failed page_id=%s recipient=%s status=%s body=%s",
                page_id,
                recipient_psid[:8],
                response.status_code,
                response.text,
            )
        response.raise_for_status()
        data = response.json()

        logger.info(
            "facebook_message_sent page_id=%s recipient=%s... message_id=%s",
            page_id,
            recipient_psid[:8],
            data.get("message_id"),
        )

        return {
            "message_id": data.get("message_id"),
            "recipient_id": data.get("recipient_id"),
        }


async def send_instagram_message(
    db: Session,
    recipient_igsid: str,
    message_text: str,
    target: IntegrationTarget | None = None,
    account_id: str | None = None,
) -> dict:
    """Send a message via Instagram DM.

    Args:
        db: Database session
        recipient_igsid: Recipient's Instagram-Scoped ID
        message_text: Message text to send
        target: Optional IntegrationTarget (auto-resolved if not provided)
        account_id: Optional Instagram Business Account ID to send from

    Returns:
        Dict with 'message_id' and 'recipient_id' from Meta API

    Raises:
        ValueError: If no active token found or token expired
        httpx.HTTPStatusError: If API request fails
    """
    override_token = _get_meta_access_token_override(db)
    token = _get_token_for_channel(
        db, ChannelType.instagram_dm, target, account_id=account_id
    )
    _ensure_token_scopes(token, _INSTAGRAM_REQUIRED_SCOPES, "instagram_dm")
    if not token and not override_token:
        raise ValueError("No active Instagram Business Account token found")

    if token:
        logger.info(
            "instagram_token_status account_id=%s expires_at=%s expired=%s",
            token.external_account_id,
            token.token_expires_at,
            token.is_token_expired(),
        )

    if token and not override_token and token.is_token_expired():
        raise ValueError(
            f"Instagram token has expired. Please reconnect. "
            f"(Account: {token.external_account_name})"
        )

    ig_account_id = account_id or (token.external_account_id if token else None)
    if not ig_account_id:
        raise ValueError("No Instagram account ID available for message send")
    if override_token and not token:
        raise ValueError("No linked Instagram Business token found for override send")

    payload = {
        "recipient": {"id": recipient_igsid},
        "message": {"text": message_text},
    }

    base_url = _get_meta_graph_base_url(db)
    access_token = override_token or (token.access_token if token else None)
    if not access_token:
        raise ValueError("No access token available for Instagram message send")
    async with httpx.AsyncClient() as client:
        response = await _post_with_retry(
            client,
            f"{base_url.rstrip('/')}/{ig_account_id}/messages",
            params={"access_token": access_token},
            json=payload,
            timeout=30,
        )
        if response.status_code >= 400:
            logger.error(
                "instagram_message_send_failed ig_account_id=%s recipient=%s status=%s body=%s",
                ig_account_id,
                recipient_igsid[:8],
                response.status_code,
                response.text,
            )
        response.raise_for_status()
        data = response.json()

        logger.info(
            "instagram_message_sent ig_account_id=%s recipient=%s... message_id=%s",
            ig_account_id,
            recipient_igsid[:8],
            data.get("message_id"),
        )

        return {
            "message_id": data.get("message_id"),
            "recipient_id": data.get("recipient_id"),
        }


def send_facebook_message_sync(
    db: Session,
    recipient_psid: str,
    message_text: str,
    target: IntegrationTarget | None = None,
    account_id: str | None = None,
) -> dict:
    """Synchronous wrapper for send_facebook_message.

    Use this in synchronous contexts (like FastAPI sync routes).

    Args:
        db: Database session
        recipient_psid: Recipient's Page-Scoped ID
        message_text: Message text to send
        target: Optional IntegrationTarget
        account_id: Optional Facebook Page ID to send from

    Returns:
        Dict with 'message_id' and 'recipient_id'
    """
    import asyncio
    import concurrent.futures

    coro = send_facebook_message(
        db,
        recipient_psid,
        message_text,
        target,
        account_id=account_id,
    )
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return asyncio.run(coro)
    # Running inside an event loop; execute in a dedicated thread.
    with concurrent.futures.ThreadPoolExecutor(max_workers=1) as executor:
        future = executor.submit(lambda: asyncio.run(coro))
        return future.result()


def send_instagram_message_sync(
    db: Session,
    recipient_igsid: str,
    message_text: str,
    target: IntegrationTarget | None = None,
    account_id: str | None = None,
) -> dict:
    """Synchronous wrapper for send_instagram_message.

    Use this in synchronous contexts (like FastAPI sync routes).

    Args:
        db: Database session
        recipient_igsid: Recipient's Instagram-Scoped ID
        message_text: Message text to send
        target: Optional IntegrationTarget
        account_id: Optional Instagram Business Account ID to send from

    Returns:
        Dict with 'message_id' and 'recipient_id'
    """
    import asyncio
    import concurrent.futures

    coro = send_instagram_message(
        db,
        recipient_igsid,
        message_text,
        target,
        account_id=account_id,
    )
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return asyncio.run(coro)
    # Running inside an event loop; execute in a dedicated thread.
    with concurrent.futures.ThreadPoolExecutor(max_workers=1) as executor:
        future = executor.submit(lambda: asyncio.run(coro))
        return future.result()
