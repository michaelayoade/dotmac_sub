"""Tests for avatar services."""

import asyncio
import os
import uuid
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import HTTPException, UploadFile

from app.services import avatar


# =============================================================================
# Helper Function Tests
# =============================================================================

def _run_async(coro):
    # Run coroutine in a dedicated thread to avoid nested event loops from anyio.
    with ThreadPoolExecutor(max_workers=1) as executor:
        future = executor.submit(asyncio.run, coro)
        return future.result()


class TestGetAllowedTypes:
    """Tests for get_allowed_types function."""

    def test_returns_set(self, monkeypatch):
        """Test returns a set of allowed types."""
        mock_settings = MagicMock()
        mock_settings.avatar_allowed_types = "image/jpeg,image/png,image/gif"
        monkeypatch.setattr(avatar, "settings", mock_settings)

        result = avatar.get_allowed_types()
        assert isinstance(result, set)
        assert "image/jpeg" in result
        assert "image/png" in result
        assert "image/gif" in result


class TestValidateAvatar:
    """Tests for validate_avatar function."""

    def test_valid_content_type(self, monkeypatch):
        """Test valid content type passes validation."""
        mock_settings = MagicMock()
        mock_settings.avatar_allowed_types = "image/jpeg,image/png"
        monkeypatch.setattr(avatar, "settings", mock_settings)

        mock_file = MagicMock(spec=UploadFile)
        mock_file.content_type = "image/jpeg"

        # Should not raise
        avatar.validate_avatar(mock_file)

    def test_invalid_content_type(self, monkeypatch):
        """Test invalid content type raises HTTPException."""
        mock_settings = MagicMock()
        mock_settings.avatar_allowed_types = "image/jpeg,image/png"
        monkeypatch.setattr(avatar, "settings", mock_settings)

        mock_file = MagicMock(spec=UploadFile)
        mock_file.content_type = "application/pdf"

        with pytest.raises(HTTPException) as exc_info:
            avatar.validate_avatar(mock_file)

        assert exc_info.value.status_code == 400
        assert "Invalid file type" in exc_info.value.detail


class TestGetExtension:
    """Tests for _get_extension function."""

    def test_jpeg(self):
        """Test JPEG extension."""
        assert avatar._get_extension("image/jpeg") == ".jpg"

    def test_png(self):
        """Test PNG extension."""
        assert avatar._get_extension("image/png") == ".png"

    def test_gif(self):
        """Test GIF extension."""
        assert avatar._get_extension("image/gif") == ".gif"

    def test_webp(self):
        """Test WebP extension."""
        assert avatar._get_extension("image/webp") == ".webp"

    def test_unknown_defaults_to_jpg(self):
        """Test unknown type defaults to .jpg."""
        assert avatar._get_extension("image/unknown") == ".jpg"


# =============================================================================
# Save Avatar Tests
# =============================================================================


class TestSaveAvatar:
    """Tests for save_avatar function."""

    def test_save_avatar_success(self, tmp_path, monkeypatch):
        """Test successful avatar save."""
        mock_settings = MagicMock()
        mock_settings.avatar_allowed_types = "image/jpeg,image/png"
        mock_settings.avatar_upload_dir = str(tmp_path)
        mock_settings.avatar_max_size_bytes = 10 * 1024 * 1024  # 10MB
        mock_settings.avatar_url_prefix = "/avatars"
        monkeypatch.setattr(avatar, "settings", mock_settings)

        mock_file = AsyncMock(spec=UploadFile)
        mock_file.content_type = "image/jpeg"
        mock_file.read.return_value = b"fake image content"

        person_id = str(uuid.uuid4())
        result = _run_async(avatar.save_avatar(mock_file, person_id))

        assert result.startswith("/avatars/")
        assert person_id in result
        assert result.endswith(".jpg")

    def test_save_avatar_file_too_large(self, tmp_path, monkeypatch):
        """Test file too large raises HTTPException."""
        mock_settings = MagicMock()
        mock_settings.avatar_allowed_types = "image/jpeg"
        mock_settings.avatar_upload_dir = str(tmp_path)
        mock_settings.avatar_max_size_bytes = 100  # Very small limit
        monkeypatch.setattr(avatar, "settings", mock_settings)

        mock_file = AsyncMock(spec=UploadFile)
        mock_file.content_type = "image/jpeg"
        mock_file.read.return_value = b"x" * 200  # Exceeds limit

        with pytest.raises(HTTPException) as exc_info:
            _run_async(avatar.save_avatar(mock_file, "test-id"))

        assert exc_info.value.status_code == 400
        assert "File too large" in exc_info.value.detail

    def test_save_avatar_creates_directory(self, tmp_path, monkeypatch):
        """Test avatar save creates upload directory if needed."""
        upload_dir = tmp_path / "avatars" / "subdir"
        mock_settings = MagicMock()
        mock_settings.avatar_allowed_types = "image/jpeg"
        mock_settings.avatar_upload_dir = str(upload_dir)
        mock_settings.avatar_max_size_bytes = 10 * 1024 * 1024
        mock_settings.avatar_url_prefix = "/avatars"
        monkeypatch.setattr(avatar, "settings", mock_settings)

        mock_file = AsyncMock(spec=UploadFile)
        mock_file.content_type = "image/jpeg"
        mock_file.read.return_value = b"fake content"

        _run_async(avatar.save_avatar(mock_file, "test-id"))

        assert upload_dir.exists()


# =============================================================================
# Delete Avatar Tests
# =============================================================================


class TestDeleteAvatar:
    """Tests for delete_avatar function."""

    def test_delete_avatar_none(self, monkeypatch):
        """Test deleting None avatar does nothing."""
        mock_settings = MagicMock()
        mock_settings.avatar_url_prefix = "/avatars"
        monkeypatch.setattr(avatar, "settings", mock_settings)

        # Should not raise
        avatar.delete_avatar(None)

    def test_delete_avatar_empty_string(self, monkeypatch):
        """Test deleting empty string does nothing."""
        mock_settings = MagicMock()
        mock_settings.avatar_url_prefix = "/avatars"
        monkeypatch.setattr(avatar, "settings", mock_settings)

        # Should not raise
        avatar.delete_avatar("")

    def test_delete_avatar_external_url(self, monkeypatch):
        """Test deleting external URL does nothing."""
        mock_settings = MagicMock()
        mock_settings.avatar_url_prefix = "/avatars"
        mock_settings.avatar_upload_dir = "/uploads/avatars"
        monkeypatch.setattr(avatar, "settings", mock_settings)

        # Should not raise for external URL
        avatar.delete_avatar("https://example.com/avatar.jpg")

    def test_delete_avatar_success(self, tmp_path, monkeypatch):
        """Test successful avatar deletion."""
        # Create a test file
        avatar_file = tmp_path / "test_avatar.jpg"
        avatar_file.write_bytes(b"fake image")

        mock_settings = MagicMock()
        mock_settings.avatar_url_prefix = "/avatars"
        mock_settings.avatar_upload_dir = str(tmp_path)
        monkeypatch.setattr(avatar, "settings", mock_settings)

        assert avatar_file.exists()

        avatar.delete_avatar("/avatars/test_avatar.jpg")

        assert not avatar_file.exists()

    def test_delete_avatar_file_not_exists(self, tmp_path, monkeypatch):
        """Test deleting non-existent file does not raise."""
        mock_settings = MagicMock()
        mock_settings.avatar_url_prefix = "/avatars"
        mock_settings.avatar_upload_dir = str(tmp_path)
        monkeypatch.setattr(avatar, "settings", mock_settings)

        # Should not raise even if file doesn't exist
        avatar.delete_avatar("/avatars/nonexistent.jpg")
