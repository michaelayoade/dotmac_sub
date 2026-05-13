"""Tests for ONT type adapter registry (transforms only)."""

from app.services.adapters.ont_types import ont_type_registry
from app.services.adapters.ont_types.base import OntTypeAdapter


class TestOntTypeRegistry:
    """Test ONT type adapter registry."""

    def test_huawei_adapters_registered(self):
        """Verify Huawei adapters are registered."""
        names = ont_type_registry.names()
        assert "huawei-hg8245h" in names
        assert "huawei-hg8546m" in names
        assert "huawei-eg8145v5" in names
        assert "huawei-generic-tr098" in names

    def test_get_by_name(self):
        """Get adapter by exact name."""
        adapter = ont_type_registry.get("huawei-hg8245h")
        assert adapter is not None
        assert adapter.vendor == "Huawei"

    def test_get_none_for_missing(self):
        """Return None if adapter not found."""
        adapter = ont_type_registry.get("nonexistent")
        assert adapter is None

    def test_get_none_for_none_input(self):
        """Return None if name is None."""
        adapter = ont_type_registry.get(None)
        assert adapter is None

    def test_security_mode_transform_wpa2(self):
        """Test WPA2 security mode transformation."""
        adapter = ont_type_registry.get("huawei-hg8245h")
        assert adapter is not None
        assert adapter.transform_security_mode("WPA2") == "11i"
        assert adapter.transform_security_mode("WPA2-Personal") == "11i"
        assert adapter.transform_security_mode("WPA2-PSK") == "11i"

    def test_security_mode_transform_wpa(self):
        """Test WPA security mode transformation."""
        adapter = ont_type_registry.get("huawei-hg8245h")
        assert adapter is not None
        assert adapter.transform_security_mode("WPA") == "WPA"
        assert adapter.transform_security_mode("WPA-Personal") == "WPA"

    def test_security_mode_transform_mixed(self):
        """Test mixed WPA/WPA2 security mode transformation."""
        adapter = ont_type_registry.get("huawei-hg8245h")
        assert adapter is not None
        assert adapter.transform_security_mode("WPA+WPA2") == "WPAand11i"
        assert adapter.transform_security_mode("Mixed") == "WPAand11i"

    def test_security_mode_transform_none(self):
        """Test None/Open security mode transformation."""
        adapter = ont_type_registry.get("huawei-hg8245h")
        assert adapter is not None
        assert adapter.transform_security_mode("None") == "None"
        assert adapter.transform_security_mode("Open") == "None"

    def test_security_mode_passthrough(self):
        """Unknown modes pass through unchanged."""
        adapter = ont_type_registry.get("huawei-hg8245h")
        assert adapter is not None
        assert adapter.transform_security_mode("UnknownMode") == "UnknownMode"

    def test_wpa3_support_on_newer_model(self):
        """Test WPA3 support on EG8145V5."""
        adapter = ont_type_registry.get("huawei-eg8145v5")
        assert adapter is not None
        assert adapter.transform_security_mode("WPA3") == "WPA3-SAE"
        assert adapter.transform_security_mode("WPA2+WPA3") == "11iandWPA3"

    def test_all_adapters(self):
        """Test all() returns adapters."""
        adapters = ont_type_registry.all()
        assert len(adapters) >= 4  # At least the 4 Huawei adapters
        assert all(isinstance(a, OntTypeAdapter) for a in adapters)


class TestOntTypeAdapter:
    """Test OntTypeAdapter dataclass."""

    def test_create_custom_adapter(self):
        """Test creating a custom adapter."""
        custom = OntTypeAdapter(
            name="test-custom",
            vendor="TestVendor",
            security_mode_map={"Custom": "CUSTOM"},
            notes="Test adapter",
        )
        assert custom.name == "test-custom"
        assert custom.vendor == "TestVendor"
        assert custom.transform_security_mode("Custom") == "CUSTOM"
        assert custom.transform_security_mode("Other") == "Other"

    def test_empty_security_map(self):
        """Adapter with no security map passes through all modes."""
        adapter = OntTypeAdapter(
            name="passthrough",
            vendor="Test",
        )
        assert adapter.transform_security_mode("WPA2") == "WPA2"
        assert adapter.transform_security_mode("11i") == "11i"
