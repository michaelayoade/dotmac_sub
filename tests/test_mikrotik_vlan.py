"""Tests for MikroTik VLAN provisioning module."""

from unittest.mock import MagicMock, patch

import pytest

from app.services.nas._mikrotik_vlan import (
    VlanProvisioningResult,
    ensure_pppoe_server,
    ensure_vlan_interface,
    ensure_vlan_ip_address,
    get_vlan_status,
    list_pppoe_servers,
    list_vlan_interfaces,
    provision_vlan_full,
    remove_vlan_interface,
)


def test_module_importable() -> None:
    """All public functions should be importable."""
    assert callable(ensure_vlan_interface)
    assert callable(ensure_vlan_ip_address)
    assert callable(ensure_pppoe_server)
    assert callable(provision_vlan_full)
    assert callable(remove_vlan_interface)
    assert callable(get_vlan_status)
    assert callable(list_vlan_interfaces)
    assert callable(list_pppoe_servers)


def test_vlan_provisioning_result_dataclass() -> None:
    """VlanProvisioningResult should have correct defaults."""
    result = VlanProvisioningResult(success=True, message="ok")
    assert result.success is True
    assert result.message == "ok"
    assert result.created is False
    assert result.details is None


def test_ensure_vlan_interface_rejects_invalid_vlan_id() -> None:
    """Invalid VLAN IDs should be rejected without API call."""
    device = MagicMock()
    result = ensure_vlan_interface(device, vlan_id=0, parent_interface="ether1")
    assert result.success is False
    assert "Invalid VLAN ID" in result.message

    result = ensure_vlan_interface(device, vlan_id=4095, parent_interface="ether1")
    assert result.success is False


@patch("app.services.nas._mikrotik_vlan._get_api")
def test_ensure_vlan_interface_creates_when_missing(mock_get_api: MagicMock) -> None:
    """Should create VLAN when it doesn't exist."""
    mock_api = MagicMock()
    mock_pool = MagicMock()
    mock_pool.get_api.return_value = mock_api
    mock_get_api.return_value = mock_pool

    vlan_resource = MagicMock()
    vlan_resource.get.return_value = []  # No existing VLANs
    mock_api.get_resource.return_value = vlan_resource

    device = MagicMock()
    device.name = "Test-NAS"

    result = ensure_vlan_interface(device, vlan_id=203, parent_interface="ether3")
    assert result.success is True
    assert result.created is True
    assert "203" in result.message
    vlan_resource.add.assert_called_once()
    mock_pool.disconnect.assert_called_once()


@patch("app.services.nas._mikrotik_vlan._get_api")
def test_ensure_vlan_interface_skips_when_exists(mock_get_api: MagicMock) -> None:
    """Should not create VLAN when it already exists."""
    mock_api = MagicMock()
    mock_pool = MagicMock()
    mock_pool.get_api.return_value = mock_api
    mock_get_api.return_value = mock_pool

    vlan_resource = MagicMock()
    vlan_resource.get.return_value = [
        {"name": "vlan203", "vlan-id": "203", "interface": "ether3"}
    ]
    mock_api.get_resource.return_value = vlan_resource

    device = MagicMock()
    device.name = "Test-NAS"

    result = ensure_vlan_interface(device, vlan_id=203, parent_interface="ether3")
    assert result.success is True
    assert result.created is False
    assert "already exists" in result.message
    vlan_resource.add.assert_not_called()


@patch("app.services.nas._mikrotik_vlan._get_api")
def test_ensure_pppoe_server_creates_when_missing(mock_get_api: MagicMock) -> None:
    """Should create PPPoE server when not bound to interface."""
    mock_api = MagicMock()
    mock_pool = MagicMock()
    mock_pool.get_api.return_value = mock_api
    mock_get_api.return_value = mock_pool

    pppoe_resource = MagicMock()
    pppoe_resource.get.return_value = []  # No existing servers
    mock_api.get_resource.return_value = pppoe_resource

    device = MagicMock()
    device.name = "Test-NAS"

    result = ensure_pppoe_server(device, interface_name="vlan203")
    assert result.success is True
    assert result.created is True
    pppoe_resource.add.assert_called_once()


@patch("app.services.nas._mikrotik_vlan._get_api")
def test_ensure_pppoe_server_skips_when_exists(mock_get_api: MagicMock) -> None:
    """Should not create PPPoE server when already bound."""
    mock_api = MagicMock()
    mock_pool = MagicMock()
    mock_pool.get_api.return_value = mock_api
    mock_get_api.return_value = mock_pool

    pppoe_resource = MagicMock()
    pppoe_resource.get.return_value = [
        {"interface": "vlan203", "service-name": "internet"}
    ]
    mock_api.get_resource.return_value = pppoe_resource

    device = MagicMock()
    device.name = "Test-NAS"

    result = ensure_pppoe_server(device, interface_name="vlan203")
    assert result.success is True
    assert result.created is False
    assert "already bound" in result.message


@patch("app.services.nas._mikrotik_vlan.ensure_pppoe_server")
@patch("app.services.nas._mikrotik_vlan.ensure_vlan_ip_address")
@patch("app.services.nas._mikrotik_vlan.ensure_vlan_interface")
def test_provision_vlan_full_orchestrates_all_steps(
    mock_vlan: MagicMock,
    mock_ip: MagicMock,
    mock_pppoe: MagicMock,
) -> None:
    """provision_vlan_full should call all three ensure functions."""
    mock_vlan.return_value = VlanProvisioningResult(success=True, message="ok", created=True)
    mock_ip.return_value = VlanProvisioningResult(success=True, message="ok", created=True)
    mock_pppoe.return_value = VlanProvisioningResult(success=True, message="ok", created=True)

    device = MagicMock()
    device.name = "Test-NAS"

    result = provision_vlan_full(
        device,
        vlan_id=203,
        parent_interface="ether3",
        ip_address="172.16.110.1/24",
    )
    assert result.success is True
    assert result.created is True
    assert "VLAN 203" in result.message
    mock_vlan.assert_called_once()
    mock_ip.assert_called_once()
    mock_pppoe.assert_called_once()


@patch("app.services.nas._mikrotik_vlan.ensure_vlan_ip_address")
@patch("app.services.nas._mikrotik_vlan.ensure_vlan_interface")
def test_provision_vlan_full_stops_on_failure(
    mock_vlan: MagicMock,
    mock_ip: MagicMock,
) -> None:
    """provision_vlan_full should stop on first failure."""
    mock_vlan.return_value = VlanProvisioningResult(success=False, message="SSH error")

    device = MagicMock()
    device.name = "Test-NAS"

    result = provision_vlan_full(
        device,
        vlan_id=203,
        parent_interface="ether3",
        ip_address="172.16.110.1/24",
    )
    assert result.success is False
    mock_ip.assert_not_called()
