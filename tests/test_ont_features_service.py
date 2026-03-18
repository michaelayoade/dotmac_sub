"""Tests for ONT feature toggle service (Phase 6)."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from app.services.network.ont_action_common import ActionResult
from app.services.network.ont_features import OntFeatureService, _check_capability


class TestCapabilityCheck:
    """Capability gate tests."""

    def test_no_vendor_returns_error(self):
        ont = MagicMock(vendor=None, model=None)
        db = MagicMock()
        result = _check_capability(db, ont, "wifi")
        assert result is not None
        assert result.success is False

    def test_no_capability_record_allows(self):
        """If no vendor capability exists, allow by default."""
        ont = MagicMock(vendor="Huawei", model="EG8145V5", firmware_version=None)
        db = MagicMock()
        with patch(
            "app.services.network.vendor_capabilities.VendorCapabilities.resolve_capability",
            return_value=None,
        ):
            result = _check_capability(db, ont, "wifi")
            assert result is None  # No error = allowed

    def test_unsupported_feature_returns_error(self):
        ont = MagicMock(vendor="Huawei", model="HG8310M", firmware_version=None)
        cap = MagicMock(supported_features={"wifi": False, "voip": False})
        db = MagicMock()
        with patch(
            "app.services.network.vendor_capabilities.VendorCapabilities.resolve_capability",
            return_value=cap,
        ):
            result = _check_capability(db, ont, "wifi")
            assert result is not None
            assert result.success is False
            assert "not supported" in result.message

    def test_supported_feature_returns_none(self):
        ont = MagicMock(vendor="Huawei", model="EG8145V5", firmware_version=None)
        cap = MagicMock(supported_features={"wifi": True})
        db = MagicMock()
        with patch(
            "app.services.network.vendor_capabilities.VendorCapabilities.resolve_capability",
            return_value=cap,
        ):
            result = _check_capability(db, ont, "wifi")
            assert result is None


class TestToggleVoip:
    """VoIP toggle tests."""

    @patch("app.services.network.ont_features._emit_feature_event")
    @patch("app.services.network.ont_features._check_capability", return_value=None)
    @patch("app.services.network.ont_features.get_ont_or_error")
    def test_enables_voip(self, mock_get, mock_cap, mock_emit):
        ont = MagicMock()
        mock_get.return_value = (ont, None)
        db = MagicMock()
        result = OntFeatureService.toggle_voip(db, "ont-1", enabled=True)
        assert result.success is True
        assert ont.voip_enabled is True
        db.commit.assert_called()

    @patch("app.services.network.ont_features._check_capability")
    @patch("app.services.network.ont_features.get_ont_or_error")
    def test_unsupported_returns_error(self, mock_get, mock_cap):
        ont = MagicMock()
        mock_get.return_value = (ont, None)
        mock_cap.return_value = ActionResult(success=False, message="Not supported")
        db = MagicMock()
        result = OntFeatureService.toggle_voip(db, "ont-1", enabled=True)
        assert result.success is False


class TestToggleWanRemoteAccess:
    """WAN remote access toggle tests."""

    @patch("app.services.network.ont_features._emit_feature_event")
    @patch("app.services.network.ont_features.get_ont_or_error")
    def test_enables_remote_access(self, mock_get, mock_emit):
        ont = MagicMock()
        mock_get.return_value = (ont, None)
        db = MagicMock()
        result = OntFeatureService.toggle_wan_remote_access(
            db, "ont-1", enabled=True
        )
        assert result.success is True
        assert ont.wan_remote_access is True


class TestOntNotFound:
    """ONT not found returns error for all feature methods."""

    def test_set_wifi_not_found(self):
        db = MagicMock()
        db.get.return_value = None
        result = OntFeatureService.set_wifi_config(db, "missing", ssid="Test")
        assert result.success is False

    def test_toggle_catv_not_found(self):
        db = MagicMock()
        db.get.return_value = None
        result = OntFeatureService.toggle_catv(db, "missing", enabled=True)
        assert result.success is False
