"""Tests for Meta OAuth service."""

import uuid
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest

from app.models.connector import ConnectorConfig, ConnectorType
from app.models.integration import IntegrationTarget, IntegrationTargetType
from app.models.oauth_token import OAuthToken
from app.services import meta_oauth


# =============================================================================
# OAuth URL Building Tests
# =============================================================================


def test_build_authorization_url():
    """Test building Facebook OAuth authorization URL."""
    url = meta_oauth.build_authorization_url(
        app_id="123456789",
        redirect_uri="https://example.com/callback",
        state="test_state_123",
    )

    assert "https://www.facebook.com/v19.0/dialog/oauth" in url
    assert "client_id=123456789" in url
    assert "redirect_uri=https%3A%2F%2Fexample.com%2Fcallback" in url
    assert "state=test_state_123" in url
    assert "pages_show_list" in url
    assert "pages_messaging" in url


def test_build_authorization_url_with_instagram():
    """Test building OAuth URL with Instagram scopes."""
    url = meta_oauth.build_authorization_url(
        app_id="123456789",
        redirect_uri="https://example.com/callback",
        state="test_state",
        include_instagram=True,
    )

    assert "instagram_basic" in url
    assert "instagram_manage_messages" in url


def test_build_authorization_url_without_instagram():
    """Test building OAuth URL without Instagram scopes."""
    url = meta_oauth.build_authorization_url(
        app_id="123456789",
        redirect_uri="https://example.com/callback",
        state="test_state",
        include_instagram=False,
    )

    assert "instagram_basic" not in url
    assert "instagram_manage_messages" not in url


# =============================================================================
# OAuth State Generation Tests
# =============================================================================


def test_generate_oauth_state():
    """Test OAuth state generation is unique."""
    state1 = meta_oauth.generate_oauth_state()
    state2 = meta_oauth.generate_oauth_state()

    assert state1 != state2
    assert len(state1) > 20  # Should be reasonably long


# =============================================================================
# Token Exchange Tests
# =============================================================================


@pytest.mark.asyncio
async def test_exchange_code_for_token_success():
    """Test exchanging authorization code for token."""
    mock_response = MagicMock()
    mock_response.json.return_value = {
        "access_token": "short_lived_token",
        "token_type": "bearer",
        "expires_in": 3600,
    }
    mock_response.raise_for_status = MagicMock()

    with patch("httpx.AsyncClient") as mock_client:
        mock_instance = AsyncMock()
        mock_instance.get = AsyncMock(return_value=mock_response)
        mock_client.return_value.__aenter__ = AsyncMock(return_value=mock_instance)
        mock_client.return_value.__aexit__ = AsyncMock(return_value=None)

        result = await meta_oauth.exchange_code_for_token(
            app_id="123",
            app_secret="secret",
            redirect_uri="https://example.com/callback",
            code="auth_code_123",
        )

        assert result["access_token"] == "short_lived_token"


@pytest.mark.asyncio
async def test_exchange_for_long_lived_token_success():
    """Test exchanging short-lived token for long-lived token."""
    mock_response = MagicMock()
    mock_response.json.return_value = {
        "access_token": "long_lived_token_abc123",
        "token_type": "bearer",
        "expires_in": 5184000,  # 60 days
    }
    mock_response.raise_for_status = MagicMock()

    with patch("httpx.AsyncClient") as mock_client:
        mock_instance = AsyncMock()
        mock_instance.get = AsyncMock(return_value=mock_response)
        mock_client.return_value.__aenter__ = AsyncMock(return_value=mock_instance)
        mock_client.return_value.__aexit__ = AsyncMock(return_value=None)

        result = await meta_oauth.exchange_for_long_lived_token(
            app_id="123",
            app_secret="secret",
            short_lived_token="short_token",
        )

        assert result["access_token"] == "long_lived_token_abc123"
        assert "expires_at" in result


# =============================================================================
# Get User Pages Tests
# =============================================================================


@pytest.mark.asyncio
async def test_get_user_pages_success():
    """Test getting user's Facebook Pages."""
    mock_response = MagicMock()
    mock_response.json.return_value = {
        "data": [
            {
                "id": "page_123",
                "name": "My Business Page",
                "access_token": "page_token_abc",
                "category": "Business",
            },
            {
                "id": "page_456",
                "name": "My Other Page",
                "access_token": "page_token_def",
                "category": "Brand",
            },
        ]
    }
    mock_response.raise_for_status = MagicMock()

    with patch("httpx.AsyncClient") as mock_client:
        mock_instance = AsyncMock()
        mock_instance.get = AsyncMock(return_value=mock_response)
        mock_client.return_value.__aenter__ = AsyncMock(return_value=mock_instance)
        mock_client.return_value.__aexit__ = AsyncMock(return_value=None)

        result = await meta_oauth.get_user_pages("user_access_token")

        assert len(result) == 2
        assert result[0]["id"] == "page_123"
        assert result[1]["name"] == "My Other Page"


@pytest.mark.asyncio
async def test_get_user_pages_empty():
    """Test getting user's Pages when none exist."""
    mock_response = MagicMock()
    mock_response.json.return_value = {"data": []}
    mock_response.raise_for_status = MagicMock()

    with patch("httpx.AsyncClient") as mock_client:
        mock_instance = AsyncMock()
        mock_instance.get = AsyncMock(return_value=mock_response)
        mock_client.return_value.__aenter__ = AsyncMock(return_value=mock_instance)
        mock_client.return_value.__aexit__ = AsyncMock(return_value=None)

        result = await meta_oauth.get_user_pages("user_access_token")

        assert result == []


# =============================================================================
# Get Instagram Business Account Tests
# =============================================================================


@pytest.mark.asyncio
async def test_get_instagram_business_account_success():
    """Test getting Instagram Business Account linked to a Page."""
    mock_response = MagicMock()
    mock_response.json.return_value = {
        "instagram_business_account": {
            "id": "ig_123456",
            "username": "mybusiness",
            "profile_picture_url": "https://cdn.com/pic.jpg",
        }
    }
    mock_response.raise_for_status = MagicMock()

    with patch("httpx.AsyncClient") as mock_client:
        mock_instance = AsyncMock()
        mock_instance.get = AsyncMock(return_value=mock_response)
        mock_client.return_value.__aenter__ = AsyncMock(return_value=mock_instance)
        mock_client.return_value.__aexit__ = AsyncMock(return_value=None)

        result = await meta_oauth.get_instagram_business_account(
            "page_123", "page_token"
        )

        assert result is not None
        assert result["id"] == "ig_123456"
        assert result["username"] == "mybusiness"


@pytest.mark.asyncio
async def test_get_instagram_business_account_not_linked():
    """Test getting Instagram account when Page has none linked."""
    mock_response = MagicMock()
    mock_response.json.return_value = {}  # No instagram_business_account
    mock_response.raise_for_status = MagicMock()

    with patch("httpx.AsyncClient") as mock_client:
        mock_instance = AsyncMock()
        mock_instance.get = AsyncMock(return_value=mock_response)
        mock_client.return_value.__aenter__ = AsyncMock(return_value=mock_instance)
        mock_client.return_value.__aexit__ = AsyncMock(return_value=None)

        result = await meta_oauth.get_instagram_business_account(
            "page_123", "page_token"
        )

        assert result is None


# =============================================================================
# Store Token Tests
# =============================================================================


def test_store_page_token_creates_new(db_session):
    """Test storing a new Page token."""
    # Create connector config
    config = ConnectorConfig(
        name="Meta Connector",
        connector_type=ConnectorType.facebook,
    )
    db_session.add(config)
    db_session.commit()

    page_data = {
        "id": "page_123",
        "name": "Test Page",
        "access_token": "page_token_abc",
        "category": "Business",
    }

    expires_at = datetime.now(timezone.utc) + timedelta(days=60)

    meta_oauth.store_page_token(
        db_session,
        str(config.id),
        page_data,
        "user_token",  # Not used for pages with their own token
        expires_at,
    )

    # Verify token was stored
    token = (
        db_session.query(OAuthToken)
        .filter(OAuthToken.external_account_id == "page_123")
        .first()
    )

    assert token is not None
    assert token.provider == "meta"
    assert token.account_type == "page"
    assert token.external_account_name == "Test Page"
    assert token.access_token == "page_token_abc"
    assert token.is_active is True


def test_store_page_token_updates_existing(db_session):
    """Test storing Page token updates existing token."""
    # Create connector config
    config = ConnectorConfig(
        name="Meta Connector",
        connector_type=ConnectorType.facebook,
    )
    db_session.add(config)
    db_session.commit()

    # Create existing token
    existing_token = OAuthToken(
        connector_config_id=config.id,
        provider="meta",
        account_type="page",
        external_account_id="page_123",
        external_account_name="Old Name",
        access_token="old_token",
    )
    db_session.add(existing_token)
    db_session.commit()

    # Update with new data
    page_data = {
        "id": "page_123",
        "name": "Updated Name",
        "access_token": "new_token_xyz",
    }

    meta_oauth.store_page_token(
        db_session,
        str(config.id),
        page_data,
        "user_token",
        datetime.now(timezone.utc) + timedelta(days=60),
    )

    # Verify token was updated
    db_session.refresh(existing_token)
    assert existing_token.external_account_name == "Updated Name"
    assert existing_token.access_token == "new_token_xyz"


def test_store_instagram_token(db_session):
    """Test storing Instagram Business Account token."""
    # Create connector config
    config = ConnectorConfig(
        name="Meta Connector",
        connector_type=ConnectorType.facebook,
    )
    db_session.add(config)
    db_session.commit()

    ig_data = {
        "id": "ig_123456",
        "username": "mybusiness",
        "profile_picture_url": "https://cdn.com/pic.jpg",
    }

    expires_at = datetime.now(timezone.utc) + timedelta(days=60)

    meta_oauth.store_instagram_token(
        db_session,
        str(config.id),
        ig_data,
        "page_token_for_ig",
        expires_at,
    )

    # Verify token was stored
    token = (
        db_session.query(OAuthToken)
        .filter(OAuthToken.external_account_id == "ig_123456")
        .first()
    )

    assert token is not None
    assert token.provider == "meta"
    assert token.account_type == "instagram_business"
    assert token.external_account_name == "mybusiness"
    assert token.access_token == "page_token_for_ig"


# =============================================================================
# Token Deactivation Tests
# =============================================================================


def test_deactivate_tokens_for_connector(db_session):
    """Test deactivating all tokens for a connector."""
    # Create connector config
    config = ConnectorConfig(
        name="Meta Connector",
        connector_type=ConnectorType.facebook,
    )
    db_session.add(config)
    db_session.commit()

    # Create multiple tokens
    for i in range(3):
        token = OAuthToken(
            connector_config_id=config.id,
            provider="meta",
            account_type="page",
            external_account_id=f"page_{i}",
            access_token=f"token_{i}",
            is_active=True,
        )
        db_session.add(token)
    db_session.commit()

    # Deactivate all tokens
    count = meta_oauth.deactivate_tokens_for_connector(db_session, str(config.id))

    assert count == 3

    # Verify all tokens are inactive
    active_tokens = (
        db_session.query(OAuthToken)
        .filter(OAuthToken.connector_config_id == config.id)
        .filter(OAuthToken.is_active.is_(True))
        .count()
    )
    assert active_tokens == 0


def test_deactivate_tokens_no_tokens(db_session):
    """Test deactivating tokens when none exist."""
    count = meta_oauth.deactivate_tokens_for_connector(db_session, str(uuid.uuid4()))
    assert count == 0


# =============================================================================
# Token Refresh Tests
# =============================================================================


@pytest.mark.asyncio
async def test_refresh_long_lived_token_success():
    """Test refreshing a long-lived token."""
    mock_response = MagicMock()
    mock_response.json.return_value = {
        "access_token": "refreshed_token_xyz",
        "token_type": "bearer",
        "expires_in": 5184000,
    }
    mock_response.raise_for_status = MagicMock()

    with patch("httpx.AsyncClient") as mock_client:
        mock_instance = AsyncMock()
        mock_instance.get = AsyncMock(return_value=mock_response)
        mock_client.return_value.__aenter__ = AsyncMock(return_value=mock_instance)
        mock_client.return_value.__aexit__ = AsyncMock(return_value=None)

        result = await meta_oauth.refresh_long_lived_token(
            app_id="123",
            app_secret="secret",
            existing_token="old_long_lived_token",
        )

        assert result["access_token"] == "refreshed_token_xyz"
        assert "expires_at" in result


# =============================================================================
# Get Token for Account Tests
# =============================================================================


def test_get_token_for_page(db_session):
    """Test getting token for a specific Page."""
    # Create connector and token
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
        external_account_id="page_123",
        access_token="secret_page_token",
        is_active=True,
    )
    db_session.add(token)
    db_session.commit()

    result = meta_oauth.get_token_for_page(db_session, "page_123")

    assert result is not None
    assert result.access_token == "secret_page_token"


def test_get_token_for_page_not_found(db_session):
    """Test getting token for non-existent Page."""
    result = meta_oauth.get_token_for_page(db_session, "nonexistent_page")
    assert result is None


def test_get_token_for_page_inactive(db_session):
    """Test getting token for Page with inactive token."""
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
        external_account_id="page_inactive",
        access_token="inactive_token",
        is_active=False,
    )
    db_session.add(token)
    db_session.commit()

    result = meta_oauth.get_token_for_page(db_session, "page_inactive")
    assert result is None


def test_get_token_for_instagram(db_session):
    """Test getting token for a specific Instagram account."""
    config = ConnectorConfig(
        name="Meta Connector",
        connector_type=ConnectorType.facebook,
    )
    db_session.add(config)
    db_session.commit()

    token = OAuthToken(
        connector_config_id=config.id,
        provider="meta",
        account_type="instagram_business",
        external_account_id="ig_123",
        access_token="secret_ig_token",
        is_active=True,
    )
    db_session.add(token)
    db_session.commit()

    result = meta_oauth.get_token_for_instagram(db_session, "ig_123")

    assert result is not None
    assert result.access_token == "secret_ig_token"
