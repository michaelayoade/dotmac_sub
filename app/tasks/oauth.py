"""OAuth token refresh Celery tasks.

Handles automatic refresh of expiring OAuth tokens to maintain integrations.
"""

import time
from datetime import UTC, datetime, timedelta

from app.celery_app import celery_app
from app.db import SessionLocal
from app.logging import get_logger
from app.metrics import observe_job
from app.models.oauth_token import OAuthToken

logger = get_logger(__name__)


@celery_app.task(name="app.tasks.oauth.refresh_expiring_tokens")
def refresh_expiring_tokens(buffer_days: int = 7):
    """Refresh OAuth tokens that are expiring within the buffer period.

    This task runs daily to proactively refresh tokens before they expire.
    For Meta tokens with 60-day lifetime, we refresh when 7 days remain.

    Args:
        buffer_days: Number of days before expiry to trigger refresh (default: 7)

    Returns:
        Dict with counts of refreshed and failed tokens
    """
    start = time.monotonic()
    status = "success"
    session = SessionLocal()
    refreshed_count = 0
    error_count = 0

    try:
        # Find tokens expiring within buffer period
        expiry_threshold = datetime.now(UTC) + timedelta(days=buffer_days)

        expiring_tokens = (
            session.query(OAuthToken)
            .filter(OAuthToken.is_active.is_(True))
            .filter(OAuthToken.provider == "meta")
            .filter(OAuthToken.token_expires_at.isnot(None))
            .filter(OAuthToken.token_expires_at <= expiry_threshold)
            .all()
        )

        logger.info(
            "oauth_token_refresh_started tokens_to_refresh=%d buffer_days=%d",
            len(expiring_tokens),
            buffer_days,
        )

        for token in expiring_tokens:
            try:
                _refresh_meta_token(session, token)
                refreshed_count += 1
                logger.info(
                    "oauth_token_refreshed token_id=%s provider=%s account=%s",
                    token.id,
                    token.provider,
                    token.external_account_name,
                )
            except Exception as exc:
                error_count += 1
                token.refresh_error = str(exc)[:500]  # Truncate long errors
                session.commit()
                logger.warning(
                    "oauth_token_refresh_failed token_id=%s error=%s",
                    token.id,
                    exc,
                )

        logger.info(
            "oauth_token_refresh_completed refreshed=%d errors=%d",
            refreshed_count,
            error_count,
        )

        return {
            "refreshed": refreshed_count,
            "errors": error_count,
            "total_checked": len(expiring_tokens),
        }

    except Exception:
        status = "error"
        session.rollback()
        logger.exception("oauth_token_refresh_task_failed")
        raise
    finally:
        session.close()
        duration = time.monotonic() - start
        observe_job("oauth_token_refresh", status, duration)


def _refresh_meta_token(session, token: OAuthToken) -> None:
    """Refresh a single Meta OAuth token.

    Args:
        session: Database session
        token: OAuthToken to refresh

    Raises:
        ValueError: If required settings missing or token invalid
        Exception: If API call fails
    """
    import asyncio

    from app.services import meta_oauth

    # Get settings from database (falls back to env vars)
    settings = meta_oauth.get_meta_settings(session)
    app_id = settings.get("meta_app_id")
    app_secret = settings.get("meta_app_secret")

    if not app_id or not app_secret:
        raise ValueError("Meta App ID and App Secret required for token refresh. Configure in Settings > Comms.")

    if not token.access_token:
        raise ValueError("Token has no access_token to refresh")

    # Run async refresh in sync context
    loop = asyncio.new_event_loop()
    try:
        result = loop.run_until_complete(
            meta_oauth.refresh_long_lived_token(
                app_id,
                app_secret,
                token.access_token,
                meta_oauth._get_meta_graph_base_url(session),
            )
        )
    finally:
        loop.close()

    # Update token with new values
    token.access_token = result.get("access_token")
    token.token_expires_at = result.get("expires_at")
    token.last_refreshed_at = datetime.now(UTC)
    token.refresh_error = None

    session.commit()
    session.refresh(token)


@celery_app.task(name="app.tasks.oauth.check_token_health")
def check_token_health():
    """Check health of all OAuth tokens and report status.

    This is a monitoring task that reports on token health without
    making any changes.

    Returns:
        Dict with token health statistics
    """
    session = SessionLocal()

    try:
        now = datetime.now(UTC)

        # Count tokens by status
        total = session.query(OAuthToken).filter(OAuthToken.is_active.is_(True)).count()

        expired = (
            session.query(OAuthToken)
            .filter(OAuthToken.is_active.is_(True))
            .filter(OAuthToken.token_expires_at.isnot(None))
            .filter(OAuthToken.token_expires_at <= now)
            .count()
        )

        expiring_soon = (
            session.query(OAuthToken)
            .filter(OAuthToken.is_active.is_(True))
            .filter(OAuthToken.token_expires_at.isnot(None))
            .filter(OAuthToken.token_expires_at > now)
            .filter(OAuthToken.token_expires_at <= now + timedelta(days=7))
            .count()
        )

        has_errors = (
            session.query(OAuthToken)
            .filter(OAuthToken.is_active.is_(True))
            .filter(OAuthToken.refresh_error.isnot(None))
            .count()
        )

        healthy = total - expired - expiring_soon - has_errors

        result = {
            "total_active": total,
            "healthy": healthy,
            "expiring_soon": expiring_soon,
            "expired": expired,
            "has_refresh_errors": has_errors,
        }

        logger.info(
            "oauth_token_health_check total=%d healthy=%d expiring_soon=%d expired=%d errors=%d",
            total,
            healthy,
            expiring_soon,
            expired,
            has_errors,
        )

        return result

    except Exception:
        session.rollback()
        raise
    finally:
        session.close()
