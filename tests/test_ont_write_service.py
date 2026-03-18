"""Tests for ONT write service (Phase 5)."""

from __future__ import annotations

import uuid
from unittest.mock import MagicMock, patch

import pytest

from app.services.network.ont_action_common import ActionResult
from app.services.network.ont_write import OntWriteService

FAKE_UUID = str(uuid.uuid4())
FAKE_UUID2 = str(uuid.uuid4())


class TestUpdateSpeedProfile:
    """Speed profile update tests."""

    def test_ont_not_found(self):
        db = MagicMock()
        db.get.return_value = None
        result = OntWriteService.update_speed_profile(
            db, "missing-id", download_profile_id=FAKE_UUID
        )
        assert result.success is False
        assert "not found" in result.message.lower()

    @patch("app.services.network.ont_write._emit_ont_event")
    @patch("app.services.network.ont_write.get_ont_or_error")
    def test_updates_download_profile(self, mock_get, mock_emit):
        ont = MagicMock()
        mock_get.return_value = (ont, None)
        db = MagicMock()
        result = OntWriteService.update_speed_profile(
            db, "ont-1", download_profile_id=FAKE_UUID
        )
        assert result.success is True
        db.commit.assert_called()


class TestUpdateExternalId:
    """External ID update tests."""

    @patch("app.services.network.ont_write.get_ont_or_error")
    def test_updates_external_id(self, mock_get):
        ont = MagicMock()
        mock_get.return_value = (ont, None)
        db = MagicMock()
        result = OntWriteService.update_external_id(db, "ont-1", external_id="42")
        assert result.success is True
        assert ont.external_id == "42"
        db.commit.assert_called()

    def test_ont_not_found(self):
        db = MagicMock()
        db.get.return_value = None
        result = OntWriteService.update_external_id(db, "missing", external_id="42")
        assert result.success is False


class TestUpdateWanConfig:
    """WAN config update tests."""

    @patch("app.services.network.ont_write.get_ont_or_error")
    def test_invalid_wan_mode(self, mock_get):
        ont = MagicMock(wan_mode=None)
        mock_get.return_value = (ont, None)
        db = MagicMock()
        result = OntWriteService.update_wan_config(
            db, "ont-1", wan_mode="invalid_mode"
        )
        assert result.success is False
        assert "invalid" in result.message.lower()


class TestMoveOnt:
    """ONT move tests."""

    @patch("app.services.network.ont_write.get_ont_or_error")
    def test_target_port_not_found(self, mock_get):
        ont = MagicMock()
        mock_get.return_value = (ont, None)
        db = MagicMock()
        db.get.return_value = None  # port not found
        result = OntWriteService.move_ont(
            db, "ont-1", target_pon_port_id=FAKE_UUID
        )
        assert result.success is False
        assert "not found" in result.message.lower()
