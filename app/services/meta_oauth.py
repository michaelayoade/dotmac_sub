"""Meta (Facebook/Instagram) OAuth2 service.

Handles OAuth2 authorization flow for connecting Facebook Pages and Instagram
Business accounts to the CRM system.

Settings (configured in Admin > System > Settings > Comms):
    meta_app_id: Facebook App ID
    meta_app_secret: Facebook App Secret
    meta_webhook_verify_token: Webhook verification token
    meta_oauth_redirect_uri: OAuth callback URL (optional)
"""

import os
import secrets
from datetime import datetime, timedelta, timezone
from urllib.parse import urlencode

import httpx
from sqlalchemy.orm import Session

from app.logging import get_logger
from app.config import settings
from app.models.domain_settings import SettingDomain
from app.models.oauth_token import OAuthToken
from app.services.common import coerce_uuid
from app.services.settings_spec import resolve_value

logger = get_logger(__name__)

_DEFAULT_META_TIMEOUT = 30  # fallback when settings unavailable


def _get_meta_api_timeout(db: Session | None = None) -> int:
    """Get the Meta API timeout from settings."""
    timeout = resolve_value(db, SettingDomain.comms, "meta_api_timeout_seconds") if db else None
    return timeout if timeout else _DEFAULT_META_TIMEOUT


def get_meta_settings(db: Session) -> dict:
    """Get Meta integration settings from the database.

    Falls back to environment variables if not set in database.

    Args:
        db: Database session

    Returns:
        Dict with meta_app_id, meta_app_secret, meta_webhook_verify_token,
        and meta_oauth_redirect_uri.
    """
    from app.services import settings_spec

    return {
        "meta_app_id": settings_spec.resolve_value(
            db, SettingDomain.comms, "meta_app_id"
        ) or os.getenv("META_APP_ID"),
        "meta_app_secret": settings_spec.resolve_value(
            db, SettingDomain.comms, "meta_app_secret"
        ) or os.getenv("META_APP_SECRET"),
        "meta_webhook_verify_token": settings_spec.resolve_value(
            db, SettingDomain.comms, "meta_webhook_verify_token"
        ) or os.getenv("META_WEBHOOK_VERIFY_TOKEN"),
        "meta_oauth_redirect_uri": settings_spec.resolve_value(
            db, SettingDomain.comms, "meta_oauth_redirect_uri"
        ) or os.getenv("META_OAUTH_REDIRECT_URI"),
    }

META_OAUTH_BASE_URL = "https://www.facebook.com"


def _get_meta_graph_api_version(db: Session | None) -> str:
    version = resolve_value(db, SettingDomain.comms, "meta_graph_api_version") if db else None
    return version or settings.meta_graph_api_version


def _get_meta_graph_base_url(db: Session | None) -> str:
    version = _get_meta_graph_api_version(db)
    return f"https://graph.facebook.com/{version}"

# Required scopes for Facebook Pages (Messenger)
FACEBOOK_SCOPES = [
    "pages_show_list",
    "pages_messaging",
    "pages_read_engagement",
    "pages_manage_posts",
    "pages_read_user_content",
]

# Required scopes for Instagram Business
INSTAGRAM_SCOPES = [
    "instagram_basic",
    "instagram_manage_messages",
    "instagram_manage_comments",
    "instagram_content_publish",
]


def generate_oauth_state() -> str:
    """Generate a secure random state for OAuth CSRF protection.

    Returns:
        A URL-safe random string (32 bytes, base64 encoded).
    """
    return secrets.token_urlsafe(32)


def build_authorization_url(
    app_id: str,
    redirect_uri: str,
    state: str,
    include_instagram: bool = True,
    api_version: str | None = None,
) -> str:
    """Build the Facebook OAuth authorization URL.

    Args:
        app_id: Facebook App ID
        redirect_uri: OAuth callback URL
        state: CSRF protection state token
        include_instagram: Whether to request Instagram scopes

    Returns:
        Full authorization URL to redirect the user to.
    """
    scopes = FACEBOOK_SCOPES.copy()
    if include_instagram:
        scopes.extend(INSTAGRAM_SCOPES)

    params = {
        "client_id": app_id,
        "redirect_uri": redirect_uri,
        "state": state,
        "scope": ",".join(scopes),
        "response_type": "code",
    }
    version = api_version or settings.meta_graph_api_version
    return f"{META_OAUTH_BASE_URL}/{version}/dialog/oauth?{urlencode(params)}"


async def exchange_code_for_token(
    app_id: str,
    app_secret: str,
    redirect_uri: str,
    code: str,
    base_url: str,
) -> dict:
    """Exchange authorization code for a short-lived access token.

    Args:
        app_id: Facebook App ID
        app_secret: Facebook App Secret
        redirect_uri: OAuth callback URL (must match the one used in authorization)
        code: Authorization code from OAuth callback

    Returns:
        Dict containing 'access_token' and optionally 'expires_in'

    Raises:
        httpx.HTTPStatusError: If the API request fails
    """
    timeout = _get_meta_api_timeout()
    async with httpx.AsyncClient() as client:
        response = await client.get(
            f"{base_url.rstrip('/')}/oauth/access_token",
            params={
                "client_id": app_id,
                "client_secret": app_secret,
                "redirect_uri": redirect_uri,
                "code": code,
            },
            timeout=timeout,
        )
        response.raise_for_status()
        return response.json()


async def exchange_for_long_lived_token(
    app_id: str,
    app_secret: str,
    short_lived_token: str,
    base_url: str,
) -> dict:
    """Exchange a short-lived token for a long-lived token (60 days).

    Args:
        app_id: Facebook App ID
        app_secret: Facebook App Secret
        short_lived_token: Short-lived access token to exchange

    Returns:
        Dict containing 'access_token', 'expires_in', and computed 'expires_at'

    Raises:
        httpx.HTTPStatusError: If the API request fails
    """
    timeout = _get_meta_api_timeout()
    async with httpx.AsyncClient() as client:
        response = await client.get(
            f"{base_url.rstrip('/')}/oauth/access_token",
            params={
                "grant_type": "fb_exchange_token",
                "client_id": app_id,
                "client_secret": app_secret,
                "fb_exchange_token": short_lived_token,
            },
            timeout=timeout,
        )
        response.raise_for_status()
        data = response.json()

        # Calculate expiry datetime (usually 60 days)
        expires_in = data.get("expires_in", 5184000)  # Default 60 days in seconds
        data["expires_at"] = datetime.now(timezone.utc) + timedelta(seconds=expires_in)

        return data


async def refresh_long_lived_token(
    app_id: str,
    app_secret: str,
    existing_token: str,
    base_url: str,
) -> dict:
    """Refresh a long-lived token before it expires.

    For Meta, refreshing is done by exchanging the existing long-lived token
    for a new one using the same flow as the initial exchange.

    Args:
        app_id: Facebook App ID
        app_secret: Facebook App Secret
        existing_token: Current long-lived token

    Returns:
        Dict containing new 'access_token' and 'expires_at'

    Raises:
        httpx.HTTPStatusError: If the API request fails
    """
    return await exchange_for_long_lived_token(app_id, app_secret, existing_token, base_url)


async def get_user_pages(access_token: str, base_url: str) -> list[dict]:
    """Get list of Facebook Pages the user manages.

    Args:
        access_token: User's long-lived access token

    Returns:
        List of page dicts with 'id', 'name', 'access_token', 'category', 'picture'

    Raises:
        httpx.HTTPStatusError: If the API request fails
    """
    timeout = _get_meta_api_timeout()
    async with httpx.AsyncClient() as client:
        response = await client.get(
            f"{base_url.rstrip('/')}/me/accounts",
            params={
                "access_token": access_token,
                "fields": "id,name,access_token,category,picture",
            },
            timeout=timeout,
        )
        response.raise_for_status()
        data = response.json()
        pages = data.get("data", [])
        safe_pages = [{"id": page.get("id"), "name": page.get("name")} for page in pages]
        logger.info(
            "meta_oauth_user_pages fetched count=%d pages=%s",
            len(pages),
            safe_pages,
        )
        return pages


async def get_instagram_business_account(
    page_id: str,
    page_access_token: str,
    base_url: str,
) -> dict | None:
    """Get Instagram Business Account connected to a Facebook Page.

    Args:
        page_id: Facebook Page ID
        page_access_token: Page access token

    Returns:
        Dict with IG account details ('id', 'name', 'username', 'profile_picture_url')
        or None if no Instagram account is linked.

    Raises:
        httpx.HTTPStatusError: If the API request fails
    """
    timeout = _get_meta_api_timeout()
    async with httpx.AsyncClient() as client:
        response = await client.get(
            f"{base_url.rstrip('/')}/{page_id}",
            params={
                "access_token": page_access_token,
                "fields": "instagram_business_account{id,name,username,profile_picture_url}",
            },
            timeout=timeout,
        )
        response.raise_for_status()
        data = response.json()
        return data.get("instagram_business_account")


def store_page_token(
    db: Session,
    connector_config_id: str,
    page_data: dict,
    user_access_token: str,
    token_expires_at: datetime,
) -> OAuthToken:
    """Store or update a Facebook Page access token.

    Page tokens from /me/accounts are long-lived if the user token is long-lived.

    Args:
        db: Database session
        connector_config_id: UUID of the ConnectorConfig
        page_data: Page data dict from get_user_pages()
        user_access_token: User's long-lived token (fallback)
        token_expires_at: When the token expires

    Returns:
        OAuthToken record (created or updated)
    """
    existing = (
        db.query(OAuthToken)
        .filter(OAuthToken.connector_config_id == coerce_uuid(connector_config_id))
        .filter(OAuthToken.provider == "meta")
        .filter(OAuthToken.account_type == "page")
        .filter(OAuthToken.external_account_id == page_data["id"])
        .first()
    )

    # Page tokens from /me/accounts inherit the user token lifetime
    page_token = page_data.get("access_token") or user_access_token

    if existing:
        existing.access_token = page_token
        existing.token_expires_at = token_expires_at
        existing.external_account_name = page_data.get("name")
        existing.last_refreshed_at = datetime.now(timezone.utc)
        existing.refresh_error = None
        existing.is_active = True
        existing.metadata_ = {
            "category": page_data.get("category"),
            "picture": page_data.get("picture", {}).get("data", {}).get("url"),
        }
        db.commit()
        db.refresh(existing)
        logger.info(
            "updated_page_token page_id=%s name=%s",
            page_data["id"],
            page_data.get("name"),
        )
        return existing

    token = OAuthToken(
        connector_config_id=coerce_uuid(connector_config_id),
        provider="meta",
        account_type="page",
        external_account_id=page_data["id"],
        external_account_name=page_data.get("name"),
        access_token=page_token,
        token_type="bearer",
        token_expires_at=token_expires_at,
        scopes=FACEBOOK_SCOPES,
        last_refreshed_at=datetime.now(timezone.utc),
        metadata_={
            "category": page_data.get("category"),
            "picture": page_data.get("picture", {}).get("data", {}).get("url"),
        },
    )
    db.add(token)
    db.commit()
    db.refresh(token)

    logger.info(
        "stored_page_token page_id=%s name=%s",
        page_data["id"],
        page_data.get("name"),
    )
    return token


def store_instagram_token(
    db: Session,
    connector_config_id: str,
    ig_account: dict,
    page_access_token: str,
    token_expires_at: datetime,
) -> OAuthToken:
    """Store or update an Instagram Business Account token.

    Instagram Business accounts use the Page access token for API calls.

    Args:
        db: Database session
        connector_config_id: UUID of the ConnectorConfig
        ig_account: Instagram account data from get_instagram_business_account()
        page_access_token: The Page's access token (used for IG API calls)
        token_expires_at: When the token expires

    Returns:
        OAuthToken record (created or updated)
    """
    existing = (
        db.query(OAuthToken)
        .filter(OAuthToken.connector_config_id == coerce_uuid(connector_config_id))
        .filter(OAuthToken.provider == "meta")
        .filter(OAuthToken.account_type == "instagram_business")
        .filter(OAuthToken.external_account_id == ig_account["id"])
        .first()
    )

    account_name = ig_account.get("username") or ig_account.get("name")

    if existing:
        existing.access_token = page_access_token  # IG uses page token
        existing.token_expires_at = token_expires_at
        existing.external_account_name = account_name
        existing.last_refreshed_at = datetime.now(timezone.utc)
        existing.refresh_error = None
        existing.is_active = True
        existing.metadata_ = {
            "profile_picture_url": ig_account.get("profile_picture_url"),
        }
        db.commit()
        db.refresh(existing)
        logger.info(
            "updated_instagram_token ig_id=%s username=%s",
            ig_account["id"],
            account_name,
        )
        return existing

    token = OAuthToken(
        connector_config_id=coerce_uuid(connector_config_id),
        provider="meta",
        account_type="instagram_business",
        external_account_id=ig_account["id"],
        external_account_name=account_name,
        access_token=page_access_token,
        token_type="bearer",
        token_expires_at=token_expires_at,
        scopes=INSTAGRAM_SCOPES,
        last_refreshed_at=datetime.now(timezone.utc),
        metadata_={
            "profile_picture_url": ig_account.get("profile_picture_url"),
        },
    )
    db.add(token)
    db.commit()
    db.refresh(token)

    logger.info(
        "stored_instagram_token ig_id=%s username=%s",
        ig_account["id"],
        account_name,
    )
    return token


def get_active_page_tokens(
    db: Session,
    connector_config_id: str | None = None,
) -> list[OAuthToken]:
    """Get all active Facebook Page tokens.

    Args:
        db: Database session
        connector_config_id: Optional filter by connector

    Returns:
        List of active OAuthToken records for Facebook Pages
    """
    query = (
        db.query(OAuthToken)
        .filter(OAuthToken.provider == "meta")
        .filter(OAuthToken.account_type == "page")
        .filter(OAuthToken.is_active.is_(True))
    )
    if connector_config_id:
        query = query.filter(
            OAuthToken.connector_config_id == coerce_uuid(connector_config_id)
        )
    return query.all()


def get_active_instagram_tokens(
    db: Session,
    connector_config_id: str | None = None,
) -> list[OAuthToken]:
    """Get all active Instagram Business Account tokens.

    Args:
        db: Database session
        connector_config_id: Optional filter by connector

    Returns:
        List of active OAuthToken records for Instagram Business accounts
    """
    query = (
        db.query(OAuthToken)
        .filter(OAuthToken.provider == "meta")
        .filter(OAuthToken.account_type == "instagram_business")
        .filter(OAuthToken.is_active.is_(True))
    )
    if connector_config_id:
        query = query.filter(
            OAuthToken.connector_config_id == coerce_uuid(connector_config_id)
        )
    return query.all()


def deactivate_tokens_for_connector(
    db: Session,
    connector_config_id: str,
) -> int:
    """Deactivate all tokens for a connector.

    Args:
        db: Database session
        connector_config_id: UUID of the ConnectorConfig

    Returns:
        Number of tokens deactivated
    """
    result = (
        db.query(OAuthToken)
        .filter(OAuthToken.connector_config_id == coerce_uuid(connector_config_id))
        .filter(OAuthToken.provider == "meta")
        .update({"is_active": False})
    )
    db.commit()
    logger.info(
        "deactivated_meta_tokens connector_id=%s count=%d",
        connector_config_id,
        result,
    )
    return result


def deactivate_missing_tokens(
    db: Session,
    connector_config_id: str,
    page_ids: set[str],
    instagram_ids: set[str],
) -> dict[str, int]:
    """Deactivate tokens that are no longer returned by Meta.

    Args:
        db: Database session
        connector_config_id: UUID of the ConnectorConfig
        page_ids: Set of active Facebook Page IDs
        instagram_ids: Set of active Instagram Business Account IDs

    Returns:
        Dict with deactivated counts for pages and instagram accounts
    """
    page_query = (
        db.query(OAuthToken)
        .filter(OAuthToken.connector_config_id == coerce_uuid(connector_config_id))
        .filter(OAuthToken.provider == "meta")
        .filter(OAuthToken.account_type == "page")
    )
    if page_ids:
        page_query = page_query.filter(OAuthToken.external_account_id.notin_(page_ids))
    pages_deactivated = page_query.update({"is_active": False})

    instagram_query = (
        db.query(OAuthToken)
        .filter(OAuthToken.connector_config_id == coerce_uuid(connector_config_id))
        .filter(OAuthToken.provider == "meta")
        .filter(OAuthToken.account_type == "instagram_business")
    )
    if instagram_ids:
        instagram_query = instagram_query.filter(
            OAuthToken.external_account_id.notin_(instagram_ids)
        )
    instagram_deactivated = instagram_query.update({"is_active": False})

    db.commit()

    logger.info(
        "deactivated_missing_meta_tokens connector_id=%s pages=%d instagram=%d",
        connector_config_id,
        pages_deactivated,
        instagram_deactivated,
    )

    return {
        "pages": pages_deactivated,
        "instagram": instagram_deactivated,
    }


def get_token_for_page(
    db: Session,
    page_id: str,
) -> OAuthToken | None:
    """Get OAuth token for a specific Facebook Page by ID.

    Args:
        db: Database session
        page_id: Facebook Page ID (external_account_id)

    Returns:
        OAuthToken if found and active, None otherwise
    """
    return (
        db.query(OAuthToken)
        .filter(OAuthToken.provider == "meta")
        .filter(OAuthToken.account_type == "page")
        .filter(OAuthToken.external_account_id == page_id)
        .filter(OAuthToken.is_active.is_(True))
        .first()
    )


def get_token_for_instagram(
    db: Session,
    instagram_account_id: str,
) -> OAuthToken | None:
    """Get OAuth token for a specific Instagram Business Account by ID.

    Args:
        db: Database session
        instagram_account_id: Instagram Business Account ID (external_account_id)

    Returns:
        OAuthToken if found and active, None otherwise
    """
    return (
        db.query(OAuthToken)
        .filter(OAuthToken.provider == "meta")
        .filter(OAuthToken.account_type == "instagram_business")
        .filter(OAuthToken.external_account_id == instagram_account_id)
        .filter(OAuthToken.is_active.is_(True))
        .first()
    )
