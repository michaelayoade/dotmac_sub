"""Tests for REST protocol adapter."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from app.services.network.olt_protocol_adapters import (
    OltProtocol,
    RestProtocolAdapter,
    get_protocol_adapter,
    get_rest_adapter,
)


@pytest.fixture
def mock_olt_rest_enabled():
    """Create a mock OLT with REST API enabled."""
    return SimpleNamespace(
        id="test-olt-rest-1",
        name="REST-Enabled-OLT",
        mgmt_ip="192.168.1.100",
        api_enabled=True,
        api_url="https://olt.example.com/api",
        api_port=443,
        api_username="admin",
        api_password="enc:secret",
        api_token=None,
        api_auth_type="basic",
    )


@pytest.fixture
def mock_olt_rest_disabled():
    """Create a mock OLT with REST API disabled."""
    return SimpleNamespace(
        id="test-olt-rest-2",
        name="REST-Disabled-OLT",
        mgmt_ip="192.168.1.101",
        api_enabled=False,
        api_url=None,
        api_port=None,
        api_username=None,
        api_password=None,
        api_token=None,
        api_auth_type=None,
    )


@pytest.fixture
def mock_olt_rest_no_url():
    """Create a mock OLT with REST enabled but no URL configured."""
    return SimpleNamespace(
        id="test-olt-rest-3",
        name="REST-NoURL-OLT",
        mgmt_ip=None,
        api_enabled=True,
        api_url=None,
        api_port=443,
        api_username="admin",
        api_password="enc:secret",
        api_token=None,
        api_auth_type="basic",
    )


class TestRestProtocolAdapter:
    """Tests for RestProtocolAdapter."""

    def test_protocol_property(self, mock_olt_rest_enabled):
        adapter = RestProtocolAdapter(mock_olt_rest_enabled)
        assert adapter.protocol == OltProtocol.REST

    def test_olt_property(self, mock_olt_rest_enabled):
        adapter = RestProtocolAdapter(mock_olt_rest_enabled)
        assert adapter.olt == mock_olt_rest_enabled

    def test_capabilities_when_enabled(self, mock_olt_rest_enabled):
        adapter = RestProtocolAdapter(mock_olt_rest_enabled)
        caps = adapter.get_capabilities()

        assert caps.protocol == OltProtocol.REST
        assert caps.available is True
        assert "skeletal" in caps.reason.lower()
        # All operations should be False (skeletal implementation)
        assert caps.can_authorize is False
        assert caps.can_deauthorize is False
        assert caps.can_reboot_ont is False

    def test_capabilities_when_disabled(self, mock_olt_rest_disabled):
        adapter = RestProtocolAdapter(mock_olt_rest_disabled)
        caps = adapter.get_capabilities()

        assert caps.protocol == OltProtocol.REST
        assert caps.available is False
        assert "not enabled" in caps.reason.lower()

    def test_capabilities_when_no_url(self, mock_olt_rest_no_url):
        adapter = RestProtocolAdapter(mock_olt_rest_no_url)
        caps = adapter.get_capabilities()

        assert caps.protocol == OltProtocol.REST
        assert caps.available is False
        assert "mgmt_ip" in caps.reason.lower() or "api_url" in caps.reason.lower()

    def test_authorize_ont_not_supported(self, mock_olt_rest_enabled):
        adapter = RestProtocolAdapter(mock_olt_rest_enabled)
        result = adapter.authorize_ont("0/1/0", "HWTC12345678")

        assert result.success is False
        assert "not supported" in result.message.lower()
        assert result.protocol_used == OltProtocol.REST

    def test_fetch_running_config_not_supported(self, mock_olt_rest_enabled):
        adapter = RestProtocolAdapter(mock_olt_rest_enabled)
        result = adapter.fetch_running_config()

        assert result.success is False
        assert "not supported" in result.message.lower()
        assert result.protocol_used == OltProtocol.REST


class TestGetProtocolAdapterRest:
    """Tests for factory function with REST protocol."""

    def test_get_protocol_adapter_rest(self, mock_olt_rest_enabled):
        adapter = get_protocol_adapter(mock_olt_rest_enabled, protocol=OltProtocol.REST)
        assert isinstance(adapter, RestProtocolAdapter)
        assert adapter.protocol == OltProtocol.REST

    def test_get_protocol_adapter_rest_string(self, mock_olt_rest_enabled):
        adapter = get_protocol_adapter(mock_olt_rest_enabled, protocol="rest")
        assert isinstance(adapter, RestProtocolAdapter)

    def test_get_rest_adapter_convenience(self, mock_olt_rest_enabled):
        adapter = get_rest_adapter(mock_olt_rest_enabled)
        assert isinstance(adapter, RestProtocolAdapter)
        assert adapter.olt == mock_olt_rest_enabled


class TestRestClientIntegration:
    """Tests for OltRestClient."""

    def test_build_base_url_from_api_url(self, mock_olt_rest_enabled):
        from app.services.network.olt_rest_client import OltRestClient

        client = OltRestClient(mock_olt_rest_enabled)
        assert client._base_url == "https://olt.example.com/api"

    def test_build_base_url_from_mgmt_ip(self):
        olt = SimpleNamespace(
            id="test-olt",
            name="Test-OLT",
            mgmt_ip="192.168.1.50",
            api_enabled=True,
            api_url=None,
            api_port=8443,
            api_username="admin",
            api_password=None,
            api_token=None,
            api_auth_type="none",
        )
        from app.services.network.olt_rest_client import OltRestClient

        client = OltRestClient(olt)
        assert client._base_url == "http://192.168.1.50:8443"

    def test_build_base_url_https_for_443(self):
        olt = SimpleNamespace(
            id="test-olt",
            name="Test-OLT",
            mgmt_ip="192.168.1.50",
            api_enabled=True,
            api_url=None,
            api_port=443,
            api_username="admin",
            api_password=None,
            api_token=None,
            api_auth_type="none",
        )
        from app.services.network.olt_rest_client import OltRestClient

        client = OltRestClient(olt)
        assert client._base_url == "https://192.168.1.50:443"

    def test_raises_value_error_when_no_url(self, mock_olt_rest_no_url):
        from app.services.network.olt_rest_client import OltRestClient

        with pytest.raises(ValueError, match="no api_url or mgmt_ip"):
            OltRestClient(mock_olt_rest_no_url)

    def test_get_rest_client_raises_when_disabled(self, mock_olt_rest_disabled):
        from app.services.network.olt_rest_client import get_rest_client

        with pytest.raises(ValueError, match="not enabled"):
            get_rest_client(mock_olt_rest_disabled)

    def test_get_rest_client_returns_client(self, mock_olt_rest_enabled):
        from app.services.network.olt_rest_client import OltRestClient, get_rest_client

        client = get_rest_client(mock_olt_rest_enabled)
        assert isinstance(client, OltRestClient)
