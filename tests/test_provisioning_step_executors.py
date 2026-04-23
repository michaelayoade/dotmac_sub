"""Tests for provisioning step executors."""

from unittest.mock import MagicMock, patch

from app.services.provisioning_step_executors import (
    execute_create_olt_service_port,
    execute_ensure_nas_vlan,
)


def test_all_executors_importable() -> None:
    assert callable(execute_create_olt_service_port)
    assert callable(execute_ensure_nas_vlan)


# ── execute_create_olt_service_port ──


def test_create_olt_sp_fails_without_ont() -> None:
    db = MagicMock()
    result = execute_create_olt_service_port(db, {}, {})
    assert result.status == "failed"
    assert result.detail is not None
    assert "ONT" in result.detail


def test_create_olt_sp_fails_without_vlan_id() -> None:
    db = MagicMock()
    result = execute_create_olt_service_port(db, {"ont_unit_id": "abc"}, {})
    assert result.status == "failed"
    assert result.detail is not None
    assert "VLAN" in result.detail


@patch("app.services.network.olt_protocol_adapters.get_protocol_adapter")
@patch("app.services.web_network_service_ports._resolve_ont_olt_context")
def test_create_olt_sp_success(mock_resolve: MagicMock, mock_get_adapter: MagicMock) -> None:
    from app.services.network.olt_protocol_adapters import OltOperationResult

    db = MagicMock()
    olt = MagicMock()
    mock_resolve.return_value = (MagicMock(), olt, "0/1/3", 5)

    mock_adapter = MagicMock()
    mock_adapter.create_service_port.return_value = OltOperationResult(
        success=True,
        message="Service port 700 created",
        data={"port_index": 700},
    )
    mock_get_adapter.return_value = mock_adapter

    result = execute_create_olt_service_port(
        db, {"ont_unit_id": "abc"}, {"vlan_id": 203}
    )
    assert result.status == "ok"
    assert result.payload is not None
    assert result.payload["olt_service_port_created"] is True
    mock_adapter.create_service_port.assert_called_once()


@patch("app.services.network.olt_protocol_adapters.get_protocol_adapter")
@patch("app.services.web_network_service_ports._resolve_ont_olt_context")
def test_create_olt_sp_ssh_failure(
    mock_resolve: MagicMock, mock_get_adapter: MagicMock
) -> None:
    from app.services.network.olt_protocol_adapters import OltOperationResult

    db = MagicMock()
    olt = MagicMock()
    mock_resolve.return_value = (MagicMock(), olt, "0/1/3", 5)

    mock_adapter = MagicMock()
    mock_adapter.create_service_port.return_value = OltOperationResult(
        success=False,
        message="SSH timeout",
        data={},
    )
    mock_get_adapter.return_value = mock_adapter

    result = execute_create_olt_service_port(
        db, {"ont_unit_id": "abc"}, {"vlan_id": 203}
    )
    assert result.status == "failed"
    assert result.detail is not None
    assert "SSH timeout" in result.detail


# ── execute_ensure_nas_vlan ──


def test_ensure_nas_vlan_fails_without_device() -> None:
    db = MagicMock()
    result = execute_ensure_nas_vlan(db, {}, {})
    assert result.status == "failed"
    assert result.detail is not None
    assert "NAS device" in result.detail


def test_ensure_nas_vlan_fails_without_vlan_id() -> None:
    db = MagicMock()
    result = execute_ensure_nas_vlan(
        db, {}, {"nas_device_id": "abc", "ip_address": "10.0.0.1/24"}
    )
    assert result.status == "failed"
    assert result.detail is not None
    assert "VLAN ID" in result.detail


def test_ensure_nas_vlan_fails_without_ip() -> None:
    db = MagicMock()
    result = execute_ensure_nas_vlan(db, {}, {"nas_device_id": "abc", "vlan_id": 203})
    assert result.status == "failed"
    assert result.detail is not None
    assert "IP address" in result.detail


def test_ensure_nas_vlan_fails_device_not_found() -> None:
    db = MagicMock()
    db.get.return_value = None
    result = execute_ensure_nas_vlan(
        db, {}, {"nas_device_id": "abc", "vlan_id": 203, "ip_address": "10.0.0.1/24"}
    )
    assert result.status == "failed"
    assert result.detail is not None
    assert "not found" in result.detail


@patch("app.services.nas._mikrotik_vlan.provision_vlan_full")
def test_ensure_nas_vlan_success(mock_provision: MagicMock) -> None:
    from app.services.nas._mikrotik_vlan import VlanProvisioningResult

    db = MagicMock()
    nas = MagicMock()
    db.get.return_value = nas
    mock_provision.return_value = VlanProvisioningResult(
        success=True, message="VLAN 203 provisioned", created=True
    )

    result = execute_ensure_nas_vlan(
        db, {}, {"nas_device_id": "abc", "vlan_id": 203, "ip_address": "10.0.0.1/24"}
    )
    assert result.status == "ok"
    assert result.payload is not None
    assert result.payload["nas_vlan_provisioned"] is True
    mock_provision.assert_called_once()

