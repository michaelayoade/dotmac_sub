"""Tests for OLT type adapter registry and capability flags."""

import pytest

from app.services.adapters.olt_types import olt_type_registry, OltCapabilities


class TestOltTypeRegistry:
    """Test OLT type adapter registry."""

    def test_huawei_adapters_registered(self):
        """Verify Huawei adapters are registered."""
        names = olt_type_registry.names()
        assert "huawei-ma5608t-v800r013" in names
        assert "huawei-ma5608t-v800r017" in names
        assert "huawei-ma5800-v800r019" in names

    def test_ma5608t_v800r013_limited_capabilities(self):
        """MA5608T V800R013 should have limited capabilities."""
        caps = olt_type_registry.get_capabilities(
            model="MA5608T",
            firmware="V800R013C00 SPC105",
        )
        assert caps.supports_ont_internet_config is False
        assert caps.supports_ont_wan_config is False
        assert caps.supports_ont_port_vlan is True

    def test_ma5608t_v800r017_full_capabilities(self):
        """MA5608T V800R017+ should have full capabilities."""
        caps = olt_type_registry.get_capabilities(
            model="MA5608T",
            firmware="V800R017C10",
        )
        assert caps.supports_ont_internet_config is True
        assert caps.supports_ont_wan_config is True

    def test_ma5800_v800r019_full_capabilities(self):
        """MA5800 V800R019 should have full capabilities."""
        caps = olt_type_registry.get_capabilities(
            model="MA5800-X7",
            firmware="V800R019C10",
        )
        assert caps.supports_ont_internet_config is True
        assert caps.supports_ont_wan_config is True
        assert caps.supports_ont_wifi_config is False  # Not until V100R019

    def test_unknown_olt_defaults_enabled(self):
        """Unknown OLTs should default to all capabilities enabled."""
        caps = olt_type_registry.get_capabilities(
            model="UnknownModel",
            firmware="V999R999",
        )
        # Default OltCapabilities has everything True except wifi
        assert caps.supports_ont_internet_config is True
        assert caps.supports_ont_wan_config is True

    def test_find_adapter_by_model_and_firmware(self):
        """Find adapter matching both model and firmware."""
        adapter = olt_type_registry.find(
            model="MA5608T",
            firmware="V800R013C00",
        )
        assert adapter is not None
        assert adapter.name == "huawei-ma5608t-v800r013"

    def test_find_adapter_firmware_pattern_matching(self):
        """Firmware patterns should use regex matching."""
        adapter = olt_type_registry.find(
            model="MA5608T",
            firmware="V800R013C00 SPC105",  # Full version string
        )
        assert adapter is not None
        assert adapter.name == "huawei-ma5608t-v800r013"

    def test_adapter_order_matters(self):
        """More specific adapters should be checked first."""
        # V800R013 should match before generic
        adapter = olt_type_registry.find(
            model="MA5608T",
            firmware="V800R013",
        )
        assert adapter is not None
        assert adapter.name == "huawei-ma5608t-v800r013"

        # V800R017 should match v800r017 adapter
        adapter = olt_type_registry.find(
            model="MA5608T",
            firmware="V800R017",
        )
        assert adapter is not None
        assert adapter.name == "huawei-ma5608t-v800r017"


class TestOltCapabilities:
    """Test OltCapabilities dataclass."""

    def test_default_capabilities(self):
        """Default capabilities should have most features enabled."""
        caps = OltCapabilities()
        assert caps.supports_ont_internet_config is True
        assert caps.supports_ont_wan_config is True
        assert caps.supports_ont_wifi_config is False  # Conservative default
        assert caps.supports_ont_port_vlan is True
        assert caps.supports_traffic_table is True

    def test_limited_capabilities(self):
        """Can create limited capability set."""
        caps = OltCapabilities(
            supports_ont_internet_config=False,
            supports_ont_wan_config=False,
        )
        assert caps.supports_ont_internet_config is False
        assert caps.supports_ont_wan_config is False
