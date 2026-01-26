"""Admin routes for Meta (Facebook/Instagram) OAuth connection.

Handles the OAuth flow for connecting Facebook Pages and Instagram
Business accounts to the CRM system.
"""

import os
from urllib.parse import quote

from fastapi import APIRouter, Depends, Request
from fastapi.responses import RedirectResponse
from sqlalchemy.orm import Session

from app.db import SessionLocal
from app.logging import get_logger
from app.models.connector import ConnectorConfig, ConnectorType
from app.models.integration import IntegrationTarget, IntegrationTargetType
from app.models.oauth_token import OAuthToken
from app.services import meta_oauth
from app.services import oauth_state
from app.web.auth.dependencies import require_web_auth

logger = get_logger(__name__)

router = APIRouter(prefix="/crm/meta", tags=["web-admin-meta"])


def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


def _get_or_create_meta_connector(
    db: Session,
) -> tuple[ConnectorConfig, IntegrationTarget]:
    """Get or create Meta connector config and integration target."""
    # Look for existing Facebook connector
    target = (
        db.query(IntegrationTarget)
        .join(ConnectorConfig, ConnectorConfig.id == IntegrationTarget.connector_config_id)
        .filter(IntegrationTarget.target_type == IntegrationTargetType.crm)
        .filter(ConnectorConfig.connector_type == ConnectorType.facebook)
        .order_by(IntegrationTarget.created_at.desc())
        .first()
    )

    if target and target.connector_config:
        return target.connector_config, target

    # Create new connector and target
    config = ConnectorConfig(
        name="Meta (Facebook/Instagram)",
        connector_type=ConnectorType.facebook,
    )
    db.add(config)
    db.commit()
    db.refresh(config)

    target = IntegrationTarget(
        name="Meta CRM Integration",
        target_type=IntegrationTargetType.crm,
        connector_config_id=config.id,
    )
    db.add(target)
    db.commit()
    db.refresh(target)

    logger.info(
        "created_meta_connector connector_id=%s target_id=%s",
        config.id,
        target.id,
    )

    return config, target


@router.get("/connect")
async def start_meta_oauth(
    request: Request,
    db: Session = Depends(get_db),
    _auth: dict = Depends(require_web_auth),
):
    """Start the Meta OAuth flow.

    Redirects the user to Facebook's authorization page to grant
    permissions for Pages and Instagram accounts.
    """
    # Get Meta settings from database
    settings = meta_oauth.get_meta_settings(db)
    app_id = settings.get("meta_app_id")

    if not app_id:
        logger.error("meta_oauth_start_failed reason=meta_app_id_not_configured")
        return RedirectResponse(
            url="/admin/crm/inbox/settings?meta_error=1&meta_error_detail=Meta%20App%20ID%20not%20configured.%20Go%20to%20Settings%20%3E%20Comms",
            status_code=303,
        )

    # Generate state for CSRF protection
    state = meta_oauth.generate_oauth_state()

    # Get or create connector config
    config, target = _get_or_create_meta_connector(db)

    # Store state with connector ID
    oauth_state.store_oauth_state(
        state,
        {
            "connector_config_id": str(config.id),
            "redirect_after": "/admin/crm/inbox/settings",
        },
    )

    # Build OAuth URL
    redirect_uri = settings.get("meta_oauth_redirect_uri") or (
        str(request.base_url).rstrip("/") + "/admin/crm/meta/callback"
    )

    api_version = meta_oauth._get_meta_graph_api_version(db)
    auth_url = meta_oauth.build_authorization_url(
        app_id=app_id,
        redirect_uri=redirect_uri,
        state=state,
        include_instagram=True,
        api_version=api_version,
    )

    logger.info("meta_oauth_started connector_id=%s", config.id)

    return RedirectResponse(url=auth_url, status_code=303)


@router.get("/callback")
async def meta_oauth_callback(
    request: Request,
    code: str | None = None,
    state: str | None = None,
    error: str | None = None,
    error_description: str | None = None,
    db: Session = Depends(get_db),
):
    """Handle Meta OAuth callback.

    Exchanges the authorization code for tokens and stores them
    for all connected Pages and Instagram accounts.
    """
    # Handle error from Meta
    if error:
        detail = quote(error_description or error, safe="")
        logger.warning("meta_oauth_callback_error error=%s detail=%s", error, error_description)
        return RedirectResponse(
            url=f"/admin/crm/inbox/settings?meta_error=1&meta_error_detail={detail}",
            status_code=303,
        )

    if not code or not state:
        logger.warning("meta_oauth_callback_invalid_params code=%s state=%s", bool(code), bool(state))
        return RedirectResponse(
            url="/admin/crm/inbox/settings?meta_error=1&meta_error_detail=Invalid%20callback",
            status_code=303,
        )

    # Verify state
    state_data = oauth_state.get_and_delete_oauth_state(state)
    if not state_data:
        logger.warning("meta_oauth_callback_invalid_state")
        return RedirectResponse(
            url="/admin/crm/inbox/settings?meta_error=1&meta_error_detail=Invalid%20or%20expired%20state",
            status_code=303,
        )

    connector_config_id = state_data.get("connector_config_id")
    redirect_after = state_data.get("redirect_after", "/admin/crm/inbox/settings")

    # Get Meta settings from database
    settings = meta_oauth.get_meta_settings(db)
    app_id = settings.get("meta_app_id")
    app_secret = settings.get("meta_app_secret")
    redirect_uri = settings.get("meta_oauth_redirect_uri") or (
        str(request.base_url).rstrip("/") + "/admin/crm/meta/callback"
    )

    try:
        # Exchange code for short-lived token
        base_url = meta_oauth._get_meta_graph_base_url(db)
        short_token_data = await meta_oauth.exchange_code_for_token(
            app_id,
            app_secret,
            redirect_uri,
            code,
            base_url,
        )
        short_token = short_token_data.get("access_token")

        # Exchange for long-lived token
        long_token_data = await meta_oauth.exchange_for_long_lived_token(
            app_id,
            app_secret,
            short_token,
            base_url,
        )
        long_token = long_token_data.get("access_token")
        token_expires_at = long_token_data.get("expires_at")

        # Get user's Facebook Pages
        pages = await meta_oauth.get_user_pages(long_token, base_url)

        pages_connected = 0
        instagram_connected = 0
        page_ids: set[str] = set()
        instagram_ids: set[str] = set()

        # Store tokens for each page and linked Instagram accounts
        for page in pages:
            page_ids.add(page["id"])
            meta_oauth.store_page_token(
                db, connector_config_id, page, long_token, token_expires_at
            )
            pages_connected += 1

            # Check for linked Instagram Business Account
            page_token = page.get("access_token", long_token)
            ig_account = await meta_oauth.get_instagram_business_account(
                page["id"],
                page_token,
                base_url,
            )
            if ig_account:
                instagram_ids.add(ig_account["id"])
                meta_oauth.store_instagram_token(
                    db, connector_config_id, ig_account, page_token, token_expires_at
                )
                instagram_connected += 1

        meta_oauth.deactivate_missing_tokens(
            db,
            connector_config_id,
            page_ids,
            instagram_ids,
        )

        logger.info(
            "meta_oauth_completed connector_id=%s pages=%d instagram=%d",
            connector_config_id,
            pages_connected,
            instagram_connected,
        )

        return RedirectResponse(
            url=f"{redirect_after}?meta_setup=1&pages={pages_connected}&instagram={instagram_connected}",
            status_code=303,
        )

    except Exception as exc:
        logger.exception("meta_oauth_callback_failed error=%s", exc)
        detail = quote(str(exc)[:100], safe="")  # Truncate for URL safety
        return RedirectResponse(
            url=f"{redirect_after}?meta_error=1&meta_error_detail={detail}",
            status_code=303,
        )


@router.post("/disconnect")
async def disconnect_meta(
    request: Request,
    db: Session = Depends(get_db),
    _auth: dict = Depends(require_web_auth),
):
    """Disconnect Meta integration.

    Deactivates all stored tokens for the Meta connector.
    """
    # Find Meta connector
    target = (
        db.query(IntegrationTarget)
        .join(ConnectorConfig, ConnectorConfig.id == IntegrationTarget.connector_config_id)
        .filter(IntegrationTarget.target_type == IntegrationTargetType.crm)
        .filter(ConnectorConfig.connector_type == ConnectorType.facebook)
        .first()
    )

    if target and target.connector_config:
        # Deactivate all tokens
        count = meta_oauth.deactivate_tokens_for_connector(db, str(target.connector_config_id))
        logger.info(
            "meta_disconnected connector_id=%s tokens_deactivated=%d",
            target.connector_config_id,
            count,
        )

    return RedirectResponse(
        url="/admin/crm/inbox/settings?meta_disconnected=1",
        status_code=303,
    )


def get_meta_connection_status(db: Session) -> dict:
    """Get Meta connection status for admin UI.

    Returns:
        Dict with connection status and connected accounts info
    """
    # Find Meta connector
    target = (
        db.query(IntegrationTarget)
        .join(ConnectorConfig, ConnectorConfig.id == IntegrationTarget.connector_config_id)
        .filter(IntegrationTarget.target_type == IntegrationTargetType.crm)
        .filter(ConnectorConfig.connector_type == ConnectorType.facebook)
        .first()
    )

    if not target or not target.connector_config:
        return {"connected": False, "pages": [], "instagram_accounts": []}

    # Get active tokens
    page_tokens = (
        db.query(OAuthToken)
        .filter(OAuthToken.connector_config_id == target.connector_config_id)
        .filter(OAuthToken.provider == "meta")
        .filter(OAuthToken.account_type == "page")
        .filter(OAuthToken.is_active.is_(True))
        .all()
    )

    instagram_tokens = (
        db.query(OAuthToken)
        .filter(OAuthToken.connector_config_id == target.connector_config_id)
        .filter(OAuthToken.provider == "meta")
        .filter(OAuthToken.account_type == "instagram_business")
        .filter(OAuthToken.is_active.is_(True))
        .all()
    )

    pages = []
    for token in page_tokens:
        metadata = token.metadata_ or {}
        pages.append({
            "id": token.external_account_id,
            "name": token.external_account_name,
            "picture": metadata.get("picture"),
            "category": metadata.get("category"),
            "expires_at": token.token_expires_at,
            "needs_refresh": token.should_refresh(),
            "has_error": bool(token.refresh_error),
        })

    instagram_accounts = []
    for token in instagram_tokens:
        metadata = token.metadata_ or {}
        instagram_accounts.append({
            "id": token.external_account_id,
            "username": token.external_account_name,
            "profile_picture_url": metadata.get("profile_picture_url"),
            "expires_at": token.token_expires_at,
            "needs_refresh": token.should_refresh(),
            "has_error": bool(token.refresh_error),
        })

    return {
        "connected": len(pages) > 0,
        "pages": pages,
        "instagram_accounts": instagram_accounts,
    }
