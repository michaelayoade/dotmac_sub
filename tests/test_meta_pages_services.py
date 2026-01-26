"""Tests for Meta pages service (posting and comments)."""

import uuid
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest

from app.models.connector import ConnectorConfig, ConnectorType
from app.models.oauth_token import OAuthToken
from app.services import meta_pages


# =============================================================================
# Facebook Page Post Tests
# =============================================================================


@pytest.mark.asyncio
async def test_create_page_post_success(db_session):
    """Test creating a Facebook Page post."""
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
        access_token="test_page_token",
        scopes=["pages_manage_posts"],
        is_active=True,
    )
    db_session.add(token)
    db_session.commit()

    mock_response = MagicMock()
    mock_response.status_code = 200
    mock_response.json.return_value = {"id": "page_123_post_456"}
    mock_response.raise_for_status = MagicMock()

    with patch("httpx.AsyncClient") as mock_client:
        mock_instance = AsyncMock()
        mock_instance.request = AsyncMock(return_value=mock_response)
        mock_client.return_value.__aenter__ = AsyncMock(return_value=mock_instance)
        mock_client.return_value.__aexit__ = AsyncMock(return_value=None)

        result = await meta_pages.create_page_post(
            db=db_session,
            page_id="page_123",
            message="Hello from our page!",
        )

        assert result["id"] == "page_123_post_456"
        mock_instance.request.assert_called_once()


@pytest.mark.asyncio
async def test_create_page_post_with_link(db_session):
    """Test creating a Page post with link."""
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
        external_account_id="page_link",
        access_token="test_token",
        scopes=["pages_manage_posts"],
        is_active=True,
    )
    db_session.add(token)
    db_session.commit()

    mock_response = MagicMock()
    mock_response.status_code = 200
    mock_response.json.return_value = {"id": "post_with_link"}
    mock_response.raise_for_status = MagicMock()

    with patch("httpx.AsyncClient") as mock_client:
        mock_instance = AsyncMock()
        mock_instance.request = AsyncMock(return_value=mock_response)
        mock_client.return_value.__aenter__ = AsyncMock(return_value=mock_instance)
        mock_client.return_value.__aexit__ = AsyncMock(return_value=None)

        result = await meta_pages.create_page_post(
            db=db_session,
            page_id="page_link",
            message="Check out this article!",
            link="https://example.com/article",
        )

        assert result["id"] == "post_with_link"
        # Verify link was included
        call_args = mock_instance.request.call_args
        assert "link" in str(call_args)


@pytest.mark.asyncio
async def test_create_page_post_no_token(db_session):
    """Test creating Page post fails without token."""
    with pytest.raises(ValueError) as exc_info:
        await meta_pages.create_page_post(
            db=db_session,
            page_id="nonexistent_page",
            message="This should fail",
        )

    assert "No active token" in str(exc_info.value)


@pytest.mark.asyncio
async def test_create_page_post_unpublished(db_session):
    """Test creating unpublished (draft) Page post."""
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
        external_account_id="page_draft",
        access_token="test_token",
        scopes=["pages_manage_posts"],
        is_active=True,
    )
    db_session.add(token)
    db_session.commit()

    mock_response = MagicMock()
    mock_response.status_code = 200
    mock_response.json.return_value = {"id": "draft_post_123"}
    mock_response.raise_for_status = MagicMock()

    with patch("httpx.AsyncClient") as mock_client:
        mock_instance = AsyncMock()
        mock_instance.request = AsyncMock(return_value=mock_response)
        mock_client.return_value.__aenter__ = AsyncMock(return_value=mock_instance)
        mock_client.return_value.__aexit__ = AsyncMock(return_value=None)

        result = await meta_pages.create_page_post(
            db=db_session,
            page_id="page_draft",
            message="Draft post",
            published=False,
        )

        assert result["id"] == "draft_post_123"


# =============================================================================
# Facebook Photo Post Tests
# =============================================================================


@pytest.mark.asyncio
async def test_create_page_photo_post(db_session):
    """Test creating a Facebook Page photo post."""
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
        external_account_id="page_photo",
        access_token="test_token",
        scopes=["pages_manage_posts"],
        is_active=True,
    )
    db_session.add(token)
    db_session.commit()

    mock_response = MagicMock()
    mock_response.status_code = 200
    mock_response.json.return_value = {"id": "photo_123", "post_id": "post_456"}
    mock_response.raise_for_status = MagicMock()

    with patch("httpx.AsyncClient") as mock_client:
        mock_instance = AsyncMock()
        mock_instance.request = AsyncMock(return_value=mock_response)
        mock_client.return_value.__aenter__ = AsyncMock(return_value=mock_instance)
        mock_client.return_value.__aexit__ = AsyncMock(return_value=None)

        result = await meta_pages.create_page_photo_post(
            db=db_session,
            page_id="page_photo",
            photo_url="https://example.com/image.jpg",
            caption="Check out this photo!",
        )

        assert result["id"] == "photo_123"


# =============================================================================
# Facebook Comment Reply Tests
# =============================================================================


@pytest.mark.asyncio
async def test_reply_to_comment_success(db_session):
    """Test replying to a Facebook comment."""
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
        external_account_id="page_comment",
        access_token="test_token",
        scopes=["pages_manage_posts"],
        is_active=True,
    )
    db_session.add(token)
    db_session.commit()

    mock_response = MagicMock()
    mock_response.status_code = 200
    mock_response.json.return_value = {"id": "reply_123"}
    mock_response.raise_for_status = MagicMock()

    with patch("httpx.AsyncClient") as mock_client:
        mock_instance = AsyncMock()
        mock_instance.request = AsyncMock(return_value=mock_response)
        mock_client.return_value.__aenter__ = AsyncMock(return_value=mock_instance)
        mock_client.return_value.__aexit__ = AsyncMock(return_value=None)

        result = await meta_pages.reply_to_comment(
            db=db_session,
            page_id="page_comment",
            comment_id="comment_456",
            message="Thanks for your comment!",
        )

        assert result["id"] == "reply_123"


@pytest.mark.asyncio
async def test_get_post_comments(db_session):
    """Test getting comments on a post."""
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
        external_account_id="page_get_comments",
        access_token="test_token",
        scopes=["pages_read_user_content"],
        is_active=True,
    )
    db_session.add(token)
    db_session.commit()

    mock_response = MagicMock()
    mock_response.status_code = 200
    mock_response.json.return_value = {
        "data": [
            {
                "id": "comment_1",
                "message": "Great post!",
                "from": {"id": "user_1", "name": "John"},
                "created_time": "2024-01-15T10:00:00+0000",
            },
            {
                "id": "comment_2",
                "message": "Thanks for sharing",
                "from": {"id": "user_2", "name": "Jane"},
                "created_time": "2024-01-15T11:00:00+0000",
            },
        ]
    }
    mock_response.raise_for_status = MagicMock()

    with patch("httpx.AsyncClient") as mock_client:
        mock_instance = AsyncMock()
        mock_instance.request = AsyncMock(return_value=mock_response)
        mock_client.return_value.__aenter__ = AsyncMock(return_value=mock_instance)
        mock_client.return_value.__aexit__ = AsyncMock(return_value=None)

        result = await meta_pages.get_post_comments(
            db=db_session,
            page_id="page_get_comments",
            post_id="post_123",
        )

        assert len(result) == 2
        assert result[0]["message"] == "Great post!"


# =============================================================================
# Instagram Post Tests
# =============================================================================


@pytest.mark.asyncio
async def test_create_instagram_image_post(db_session):
    """Test creating an Instagram image post."""
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
        access_token="test_ig_token",
        scopes=["instagram_content_publish"],
        is_active=True,
    )
    db_session.add(token)
    db_session.commit()

    # Mock container creation and publish responses
    container_response = MagicMock()
    container_response.status_code = 200
    container_response.json.return_value = {"id": "container_123"}
    container_response.raise_for_status = MagicMock()

    publish_response = MagicMock()
    publish_response.status_code = 200
    publish_response.json.return_value = {"id": "ig_media_123"}
    publish_response.raise_for_status = MagicMock()

    with patch("httpx.AsyncClient") as mock_client:
        mock_instance = AsyncMock()
        mock_instance.request = AsyncMock(
            side_effect=[container_response, publish_response]
        )
        mock_client.return_value.__aenter__ = AsyncMock(return_value=mock_instance)
        mock_client.return_value.__aexit__ = AsyncMock(return_value=None)

        result = await meta_pages.create_instagram_image_post(
            db=db_session,
            ig_account_id="ig_123",
            image_url="https://example.com/photo.jpg",
            caption="Beautiful sunset #nature #photography",
        )

        assert result["id"] == "ig_media_123"
        # Should be called twice: container + publish
        assert mock_instance.request.call_count == 2


@pytest.mark.asyncio
async def test_create_instagram_image_post_no_token(db_session):
    """Test Instagram post fails without token."""
    with pytest.raises(ValueError) as exc_info:
        await meta_pages.create_instagram_image_post(
            db=db_session,
            ig_account_id="nonexistent_ig",
            image_url="https://example.com/photo.jpg",
        )

    assert "No active token" in str(exc_info.value)


@pytest.mark.asyncio
async def test_create_instagram_carousel_post(db_session):
    """Test creating an Instagram carousel post."""
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
        external_account_id="ig_carousel",
        access_token="test_token",
        scopes=["instagram_content_publish"],
        is_active=True,
    )
    db_session.add(token)
    db_session.commit()

    # Mock responses for each step
    child1_response = MagicMock()
    child1_response.status_code = 200
    child1_response.json.return_value = {"id": "child_1"}
    child1_response.raise_for_status = MagicMock()

    child2_response = MagicMock()
    child2_response.status_code = 200
    child2_response.json.return_value = {"id": "child_2"}
    child2_response.raise_for_status = MagicMock()

    carousel_response = MagicMock()
    carousel_response.status_code = 200
    carousel_response.json.return_value = {"id": "carousel_container"}
    carousel_response.raise_for_status = MagicMock()

    publish_response = MagicMock()
    publish_response.status_code = 200
    publish_response.json.return_value = {"id": "ig_carousel_123"}
    publish_response.raise_for_status = MagicMock()

    with patch("httpx.AsyncClient") as mock_client:
        mock_instance = AsyncMock()
        mock_instance.request = AsyncMock(
            side_effect=[child1_response, child2_response, carousel_response, publish_response]
        )
        mock_client.return_value.__aenter__ = AsyncMock(return_value=mock_instance)
        mock_client.return_value.__aexit__ = AsyncMock(return_value=None)

        result = await meta_pages.create_instagram_carousel_post(
            db=db_session,
            ig_account_id="ig_carousel",
            media_urls=[
                "https://example.com/photo1.jpg",
                "https://example.com/photo2.jpg",
            ],
            caption="My carousel #slideshow",
        )

        assert result["id"] == "ig_carousel_123"
        # 2 children + 1 carousel + 1 publish = 4 calls
        assert mock_instance.request.call_count == 4


@pytest.mark.asyncio
async def test_create_instagram_carousel_invalid_count(db_session):
    """Test carousel validation for image count."""
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
        external_account_id="ig_invalid_carousel",
        access_token="test_token",
        scopes=["instagram_content_publish"],
        is_active=True,
    )
    db_session.add(token)
    db_session.commit()

    # Only 1 image - should fail
    with pytest.raises(ValueError) as exc_info:
        await meta_pages.create_instagram_carousel_post(
            db=db_session,
            ig_account_id="ig_invalid_carousel",
            media_urls=["https://example.com/single.jpg"],
        )

    assert "between 2 and 10" in str(exc_info.value)


# =============================================================================
# Instagram Comment Reply Tests
# =============================================================================


@pytest.mark.asyncio
async def test_reply_to_instagram_comment(db_session):
    """Test replying to an Instagram comment."""
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
        external_account_id="ig_reply",
        access_token="test_token",
        scopes=["instagram_manage_comments"],
        is_active=True,
    )
    db_session.add(token)
    db_session.commit()

    mock_response = MagicMock()
    mock_response.status_code = 200
    mock_response.json.return_value = {"id": "ig_reply_123"}
    mock_response.raise_for_status = MagicMock()

    with patch("httpx.AsyncClient") as mock_client:
        mock_instance = AsyncMock()
        mock_instance.request = AsyncMock(return_value=mock_response)
        mock_client.return_value.__aenter__ = AsyncMock(return_value=mock_instance)
        mock_client.return_value.__aexit__ = AsyncMock(return_value=None)

        result = await meta_pages.reply_to_instagram_comment(
            db=db_session,
            ig_account_id="ig_reply",
            comment_id="ig_comment_456",
            message="Thanks for your comment!",
        )

        assert result["id"] == "ig_reply_123"


@pytest.mark.asyncio
async def test_get_instagram_media_comments(db_session):
    """Test getting comments on Instagram media."""
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
        external_account_id="ig_get_comments",
        access_token="test_token",
        scopes=["instagram_manage_comments"],
        is_active=True,
    )
    db_session.add(token)
    db_session.commit()

    mock_response = MagicMock()
    mock_response.status_code = 200
    mock_response.json.return_value = {
        "data": [
            {
                "id": "ig_comment_1",
                "text": "Amazing photo!",
                "username": "user123",
                "timestamp": "2024-01-15T10:00:00+0000",
            },
            {
                "id": "ig_comment_2",
                "text": "Love this!",
                "username": "user456",
                "timestamp": "2024-01-15T11:00:00+0000",
            },
        ]
    }
    mock_response.raise_for_status = MagicMock()

    with patch("httpx.AsyncClient") as mock_client:
        mock_instance = AsyncMock()
        mock_instance.request = AsyncMock(return_value=mock_response)
        mock_client.return_value.__aenter__ = AsyncMock(return_value=mock_instance)
        mock_client.return_value.__aexit__ = AsyncMock(return_value=None)

        result = await meta_pages.get_instagram_media_comments(
            db=db_session,
            ig_account_id="ig_get_comments",
            media_id="ig_media_123",
        )

        assert len(result) == 2
        assert result[0]["text"] == "Amazing photo!"


# =============================================================================
# Utility Function Tests
# =============================================================================


def test_get_connected_pages(db_session):
    """Test getting list of connected Facebook Pages."""
    config = ConnectorConfig(
        name="Meta Connector",
        connector_type=ConnectorType.facebook,
    )
    db_session.add(config)
    db_session.commit()

    # Create multiple page tokens
    for i in range(3):
        token = OAuthToken(
            connector_config_id=config.id,
            provider="meta",
            account_type="page",
            external_account_id=f"page_{i}",
            external_account_name=f"Page {i}",
            access_token=f"token_{i}",
            is_active=True,
            metadata_={"category": f"Category {i}"},
        )
        db_session.add(token)
    db_session.commit()

    result = meta_pages.get_connected_pages(db_session)

    assert len(result) == 3
    assert all("page_id" in p for p in result)
    assert all("name" in p for p in result)


def test_get_connected_pages_empty(db_session):
    """Test getting connected Pages when none exist."""
    result = meta_pages.get_connected_pages(db_session)
    assert result == []


def test_get_connected_pages_excludes_inactive(db_session):
    """Test that inactive tokens are excluded."""
    config = ConnectorConfig(
        name="Meta Connector",
        connector_type=ConnectorType.facebook,
    )
    db_session.add(config)
    db_session.commit()

    # Active token
    active_token = OAuthToken(
        connector_config_id=config.id,
        provider="meta",
        account_type="page",
        external_account_id="active_page",
        external_account_name="Active Page",
        access_token="active_token",
        is_active=True,
    )
    db_session.add(active_token)

    # Inactive token
    inactive_token = OAuthToken(
        connector_config_id=config.id,
        provider="meta",
        account_type="page",
        external_account_id="inactive_page",
        external_account_name="Inactive Page",
        access_token="inactive_token",
        is_active=False,
    )
    db_session.add(inactive_token)
    db_session.commit()

    result = meta_pages.get_connected_pages(db_session)

    assert len(result) == 1
    assert result[0]["page_id"] == "active_page"


def test_get_connected_instagram_accounts(db_session):
    """Test getting list of connected Instagram accounts."""
    config = ConnectorConfig(
        name="Meta Connector",
        connector_type=ConnectorType.facebook,
    )
    db_session.add(config)
    db_session.commit()

    # Create Instagram tokens
    for i in range(2):
        token = OAuthToken(
            connector_config_id=config.id,
            provider="meta",
            account_type="instagram_business",
            external_account_id=f"ig_{i}",
            external_account_name=f"business_{i}",
            access_token=f"token_{i}",
            is_active=True,
        )
        db_session.add(token)
    db_session.commit()

    result = meta_pages.get_connected_instagram_accounts(db_session)

    assert len(result) == 2
    assert all("account_id" in a for a in result)
    assert all("username" in a for a in result)


# =============================================================================
# Error Handling Tests
# =============================================================================


@pytest.mark.asyncio
async def test_create_post_http_error(db_session):
    """Test handling HTTP error when creating post."""
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
        access_token="test_token",
        scopes=["pages_manage_posts"],
        is_active=True,
    )
    db_session.add(token)
    db_session.commit()

    with patch("httpx.AsyncClient") as mock_client:
        mock_instance = AsyncMock()
        mock_instance.request = AsyncMock(
            side_effect=httpx.HTTPStatusError(
                "Rate limited",
                request=MagicMock(),
                response=MagicMock(status_code=429),
            )
        )
        mock_client.return_value.__aenter__ = AsyncMock(return_value=mock_instance)
        mock_client.return_value.__aexit__ = AsyncMock(return_value=None)

        with pytest.raises(httpx.HTTPStatusError):
            await meta_pages.create_page_post(
                db=db_session,
                page_id="page_error",
                message="This will fail",
            )
