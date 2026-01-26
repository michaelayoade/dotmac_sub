"""Tests for OAuth Celery tasks."""

import uuid
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.models.connector import ConnectorConfig, ConnectorType
from app.models.oauth_token import OAuthToken
from app.tasks import oauth as oauth_tasks


# =============================================================================
# Refresh Expiring Tokens Tests
# =============================================================================


def test_refresh_expiring_tokens_no_tokens(db_session):
    """Test refresh task with no expiring tokens."""
    with patch("app.tasks.oauth.SessionLocal") as mock_session:
        mock_session.return_value = db_session

        result = oauth_tasks.refresh_expiring_tokens()

        assert result["refreshed"] == 0
        assert result["errors"] == 0
        assert result["total_checked"] == 0


def test_refresh_expiring_tokens_finds_expiring(db_session):
    """Test refresh task finds tokens expiring within threshold."""
    config = ConnectorConfig(
        name="Meta Connector",
        connector_type=ConnectorType.facebook,
    )
    db_session.add(config)
    db_session.commit()

    # Token expiring in 5 days (within default 7-day threshold)
    expiring_token = OAuthToken(
        connector_config_id=config.id,
        provider="meta",
        account_type="page",
        external_account_id="page_expiring",
        access_token="expiring_token",
        token_expires_at=datetime.now(timezone.utc) + timedelta(days=5),
        is_active=True,
    )
    db_session.add(expiring_token)

    # Token not expiring (30 days out)
    safe_token = OAuthToken(
        connector_config_id=config.id,
        provider="meta",
        account_type="page",
        external_account_id="page_safe",
        access_token="safe_token",
        token_expires_at=datetime.now(timezone.utc) + timedelta(days=30),
        is_active=True,
    )
    db_session.add(safe_token)
    db_session.commit()

    with patch("app.tasks.oauth.SessionLocal") as mock_session:
        mock_session.return_value = db_session

        with patch("app.tasks.oauth._refresh_meta_token") as mock_refresh:
            result = oauth_tasks.refresh_expiring_tokens()

            # Should only refresh the expiring token
            assert mock_refresh.call_count == 1
            assert result["total_checked"] == 1


def test_refresh_expiring_tokens_skips_inactive(db_session):
    """Test refresh task skips inactive tokens."""
    config = ConnectorConfig(
        name="Meta Connector",
        connector_type=ConnectorType.facebook,
    )
    db_session.add(config)
    db_session.commit()

    # Inactive token that would otherwise need refresh
    inactive_token = OAuthToken(
        connector_config_id=config.id,
        provider="meta",
        account_type="page",
        external_account_id="page_inactive",
        access_token="inactive_token",
        token_expires_at=datetime.now(timezone.utc) + timedelta(days=3),
        is_active=False,  # Inactive
    )
    db_session.add(inactive_token)
    db_session.commit()

    with patch("app.tasks.oauth.SessionLocal") as mock_session:
        mock_session.return_value = db_session

        with patch("app.tasks.oauth._refresh_meta_token") as mock_refresh:
            result = oauth_tasks.refresh_expiring_tokens()

            # Inactive tokens should not be refreshed
            mock_refresh.assert_not_called()
            assert result["total_checked"] == 0


def test_refresh_expiring_tokens_handles_error(db_session):
    """Test refresh task handles errors gracefully."""
    config = ConnectorConfig(
        name="Meta Connector",
        connector_type=ConnectorType.facebook,
    )
    db_session.add(config)
    db_session.commit()

    token = OAuthToken(
        connector_config_id=config.id,
        provider="meta",
        account_type="page",
        external_account_id="page_error",
        access_token="error_token",
        token_expires_at=datetime.now(timezone.utc) + timedelta(days=3),
        is_active=True,
    )
    db_session.add(token)
    db_session.commit()

    with patch("app.tasks.oauth.SessionLocal") as mock_session:
        mock_session.return_value = db_session

        with patch("app.tasks.oauth._refresh_meta_token") as mock_refresh:
            mock_refresh.side_effect = Exception("API Error")

            # Should not raise, just log and continue
            result = oauth_tasks.refresh_expiring_tokens()

            assert result["errors"] == 1
            assert result["refreshed"] == 0


# =============================================================================
# Check Token Health Tests
# =============================================================================


def test_check_token_health_all_healthy(db_session):
    """Test token health check with all healthy tokens."""
    config = ConnectorConfig(
        name="Meta Connector",
        connector_type=ConnectorType.facebook,
    )
    db_session.add(config)
    db_session.commit()

    # Healthy token (expires in 30 days)
    healthy_token = OAuthToken(
        connector_config_id=config.id,
        provider="meta",
        account_type="page",
        external_account_id="page_healthy",
        access_token="healthy_token",
        token_expires_at=datetime.now(timezone.utc) + timedelta(days=30),
        is_active=True,
    )
    db_session.add(healthy_token)
    db_session.commit()

    with patch("app.tasks.oauth.SessionLocal") as mock_session:
        mock_session.return_value = db_session

        result = oauth_tasks.check_token_health()

        assert result["total_active"] == 1
        assert result["healthy"] == 1
        assert result["expiring_soon"] == 0
        assert result["expired"] == 0


def test_check_token_health_expiring_soon(db_session):
    """Test token health check identifies expiring tokens."""
    config = ConnectorConfig(
        name="Meta Connector",
        connector_type=ConnectorType.facebook,
    )
    db_session.add(config)
    db_session.commit()

    # Expiring soon (5 days)
    expiring_token = OAuthToken(
        connector_config_id=config.id,
        provider="meta",
        account_type="page",
        external_account_id="page_expiring",
        access_token="expiring_token",
        token_expires_at=datetime.now(timezone.utc) + timedelta(days=5),
        is_active=True,
    )
    db_session.add(expiring_token)
    db_session.commit()

    with patch("app.tasks.oauth.SessionLocal") as mock_session:
        mock_session.return_value = db_session

        result = oauth_tasks.check_token_health()

        assert result["expiring_soon"] == 1


def test_check_token_health_expired(db_session):
    """Test token health check identifies expired tokens."""
    config = ConnectorConfig(
        name="Meta Connector",
        connector_type=ConnectorType.facebook,
    )
    db_session.add(config)
    db_session.commit()

    # Already expired
    expired_token = OAuthToken(
        connector_config_id=config.id,
        provider="meta",
        account_type="page",
        external_account_id="page_expired",
        access_token="expired_token",
        token_expires_at=datetime.now(timezone.utc) - timedelta(days=1),
        is_active=True,  # Still marked active but expired
    )
    db_session.add(expired_token)
    db_session.commit()

    with patch("app.tasks.oauth.SessionLocal") as mock_session:
        mock_session.return_value = db_session

        result = oauth_tasks.check_token_health()

        assert result["expired"] == 1


def test_check_token_health_with_errors(db_session):
    """Test token health check identifies tokens with refresh errors."""
    config = ConnectorConfig(
        name="Meta Connector",
        connector_type=ConnectorType.facebook,
    )
    db_session.add(config)
    db_session.commit()

    # Token with refresh error
    error_token = OAuthToken(
        connector_config_id=config.id,
        provider="meta",
        account_type="page",
        external_account_id="page_error",
        access_token="error_token",
        token_expires_at=datetime.now(timezone.utc) + timedelta(days=30),
        is_active=True,
        refresh_error="OAuth error: Invalid token",
    )
    db_session.add(error_token)
    db_session.commit()

    with patch("app.tasks.oauth.SessionLocal") as mock_session:
        mock_session.return_value = db_session

        result = oauth_tasks.check_token_health()

        assert result["has_refresh_errors"] == 1


# =============================================================================
# Internal Helper Tests
# =============================================================================


def test_refresh_meta_token_success(db_session):
    """Test _refresh_meta_token updates token successfully."""
    config = ConnectorConfig(
        name="Meta Connector",
        connector_type=ConnectorType.facebook,
    )
    db_session.add(config)
    db_session.commit()

    token = OAuthToken(
        connector_config_id=config.id,
        provider="meta",
        account_type="page",
        external_account_id="page_refresh",
        access_token="old_token",
        token_expires_at=datetime.now(timezone.utc) + timedelta(days=5),
        is_active=True,
    )
    db_session.add(token)
    db_session.commit()

    new_expires_at = datetime.now(timezone.utc) + timedelta(days=60)
    new_token_data = {
        "access_token": "new_refreshed_token",
        "expires_at": new_expires_at,
    }

    with patch.dict("os.environ", {"META_APP_ID": "test_app_id", "META_APP_SECRET": "test_secret"}):
        with patch("app.services.meta_oauth.refresh_long_lived_token") as mock_refresh:
            # Create async mock that returns our data
            async def async_return(*args, **kwargs):
                return new_token_data
            mock_refresh.side_effect = async_return

            oauth_tasks._refresh_meta_token(db_session, token)

    db_session.refresh(token)
    assert token.access_token == "new_refreshed_token"
    assert token.refresh_error is None


def test_refresh_meta_token_missing_env_vars(db_session):
    """Test _refresh_meta_token raises error when env vars missing."""
    config = ConnectorConfig(
        name="Meta Connector",
        connector_type=ConnectorType.facebook,
    )
    db_session.add(config)
    db_session.commit()

    token = OAuthToken(
        connector_config_id=config.id,
        provider="meta",
        account_type="page",
        external_account_id="page_fail",
        access_token="old_token",
        is_active=True,
    )
    db_session.add(token)
    db_session.commit()

    with patch.dict("os.environ", {"META_APP_ID": "", "META_APP_SECRET": ""}, clear=False):
        with pytest.raises(ValueError, match="Meta App ID and App Secret required"):
            oauth_tasks._refresh_meta_token(db_session, token)


# =============================================================================
# OAuthToken Model Tests
# =============================================================================


def test_oauth_token_is_expired(db_session):
    """Test OAuthToken.is_token_expired() method."""
    config = ConnectorConfig(
        name="Meta Connector",
        connector_type=ConnectorType.facebook,
    )
    db_session.add(config)
    db_session.commit()

    # Expired token
    expired = OAuthToken(
        connector_config_id=config.id,
        provider="meta",
        account_type="page",
        external_account_id="expired",
        access_token="token",
        token_expires_at=datetime.now(timezone.utc) - timedelta(hours=1),
        is_active=True,
    )
    db_session.add(expired)

    # Not expired token
    valid = OAuthToken(
        connector_config_id=config.id,
        provider="meta",
        account_type="page",
        external_account_id="valid",
        access_token="token",
        token_expires_at=datetime.now(timezone.utc) + timedelta(days=30),
        is_active=True,
    )
    db_session.add(valid)
    db_session.commit()

    assert expired.is_token_expired() is True
    assert valid.is_token_expired() is False


def test_oauth_token_should_refresh(db_session):
    """Test OAuthToken.should_refresh() method."""
    config = ConnectorConfig(
        name="Meta Connector",
        connector_type=ConnectorType.facebook,
    )
    db_session.add(config)
    db_session.commit()

    # Should refresh (expires in 5 days, threshold is 7)
    needs_refresh = OAuthToken(
        connector_config_id=config.id,
        provider="meta",
        account_type="page",
        external_account_id="needs_refresh",
        access_token="token",
        token_expires_at=datetime.now(timezone.utc) + timedelta(days=5),
        is_active=True,
    )
    db_session.add(needs_refresh)

    # Should not refresh (expires in 30 days)
    no_refresh = OAuthToken(
        connector_config_id=config.id,
        provider="meta",
        account_type="page",
        external_account_id="no_refresh",
        access_token="token",
        token_expires_at=datetime.now(timezone.utc) + timedelta(days=30),
        is_active=True,
    )
    db_session.add(no_refresh)
    db_session.commit()

    assert needs_refresh.should_refresh() is True
    assert no_refresh.should_refresh() is False


def test_oauth_token_days_until_expiry(db_session):
    """Test OAuthToken.days_until_expiry() method."""
    config = ConnectorConfig(
        name="Meta Connector",
        connector_type=ConnectorType.facebook,
    )
    db_session.add(config)
    db_session.commit()

    token = OAuthToken(
        connector_config_id=config.id,
        provider="meta",
        account_type="page",
        external_account_id="days_test",
        access_token="token",
        token_expires_at=datetime.now(timezone.utc) + timedelta(days=15),
        is_active=True,
    )
    db_session.add(token)
    db_session.commit()

    days = token.days_until_expiry()
    assert 14 <= days <= 15  # Allow for timing variance


def test_oauth_token_days_until_expiry_no_expiry(db_session):
    """Test days_until_expiry when no expiry date set."""
    config = ConnectorConfig(
        name="Meta Connector",
        connector_type=ConnectorType.facebook,
    )
    db_session.add(config)
    db_session.commit()

    token = OAuthToken(
        connector_config_id=config.id,
        provider="meta",
        account_type="page",
        external_account_id="no_expiry",
        access_token="token",
        token_expires_at=None,  # No expiry
        is_active=True,
    )
    db_session.add(token)
    db_session.commit()

    days = token.days_until_expiry()
    assert days is None
