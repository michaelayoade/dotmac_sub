"""Tests for ONT read facade service (Phase 4)."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

from app.services.network.ont_read import OntReadFacade, _classify_signal


class TestSignalClassification:
    """Signal quality classification tests."""

    def test_none_signal(self):
        assert _classify_signal(None) is None

    def test_good_signal(self):
        assert _classify_signal(-20.0) == "good"

    def test_warning_signal(self):
        assert _classify_signal(-26.0) == "warning"

    def test_critical_signal(self):
        assert _classify_signal(-30.0) == "critical"

    def test_boundary_good(self):
        assert _classify_signal(-25.0) == "good"

    def test_boundary_warning(self):
        assert _classify_signal(-28.0) == "warning"


class TestGetCapabilities:
    """Vendor capability resolution tests."""

    def test_empty_vendor_model(self):
        """If ONT has no vendor/model, capabilities should be empty."""
        db = MagicMock()
        db.get.return_value = MagicMock(vendor=None, model=None)
        caps = OntReadFacade.get_capabilities(db, "fake-id")
        assert caps == {}

    def test_no_matching_capability(self):
        """If no capability record matches, return empty dict."""
        ont = MagicMock(vendor="Unknown", model="XYZ", firmware_version=None)
        db = MagicMock()
        db.get.return_value = ont
        with patch(
            "app.services.network.vendor_capabilities.VendorCapabilities.resolve_capability",
            return_value=None,
        ):
            caps = OntReadFacade.get_capabilities(db, "fake-id")
            assert caps == {}

    def test_with_capability(self):
        """If capability record exists, return feature dict."""
        ont = MagicMock(vendor="Huawei", model="EG8145V5", firmware_version=None)
        cap = MagicMock(
            supported_features={"wifi": True, "voip": True, "catv": False, "iptv": False, "tr069": True},
            supports_vlan_tagging=True,
            supports_qinq=False,
            supports_ipv6=False,
        )
        db = MagicMock()
        db.get.return_value = ont
        with patch(
            "app.services.network.vendor_capabilities.VendorCapabilities.resolve_capability",
            return_value=cap,
        ):
            caps = OntReadFacade.get_capabilities(db, "fake-id")
            assert caps["wifi"] is True
            assert caps["catv"] is False
            assert caps["vlan_tagging"] is True
