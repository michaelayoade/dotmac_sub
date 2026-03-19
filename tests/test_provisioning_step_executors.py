"""Tests for provisioning step executors."""

from unittest.mock import MagicMock, patch

from app.services.provisioning_step_executors import (
    execute_create_olt_service_port,
    execute_ensure_nas_vlan,
    execute_push_tr069_pppoe_credentials,
    execute_push_tr069_wan_config,
)


def test_all_executors_importable() -> None:
    assert callable(execute_create_olt_service_port)
    assert callable(execute_ensure_nas_vlan)
    assert callable(execute_push_tr069_wan_config)
    assert callable(execute_push_tr069_pppoe_credentials)


# ── execute_create_olt_service_port ──


def test_create_olt_sp_fails_without_ont() -> None:
    db = MagicMock()
    result = execute_create_olt_service_port(db, {}, {})
    assert result.status == "failed"
    assert "ONT" in result.detail


def test_create_olt_sp_fails_without_vlan_id() -> None:
    db = MagicMock()
    result = execute_create_olt_service_port(
        db, {"ont_unit_id": "abc"}, {}
    )
    assert result.status == "failed"
    assert "VLAN" in result.detail


@patch("app.services.web_network_service_ports._resolve_ont_olt_context")
@patch("app.services.network.olt_ssh_service_ports.create_single_service_port")
def test_create_olt_sp_success(mock_create: MagicMock, mock_resolve: MagicMock) -> None:
    db = MagicMock()
    mock_resolve.return_value = {
        "olt": MagicMock(),
        "fsp": "0/1/3",
        "olt_ont_id": 5,
    }
    mock_create.return_value = (True, "Service port 700 created")

    result = execute_create_olt_service_port(
        db, {"ont_unit_id": "abc"}, {"vlan_id": 203}
    )
    assert result.status == "ok"
    assert result.payload["olt_service_port_created"] is True
    mock_create.assert_called_once()


@patch("app.services.web_network_service_ports._resolve_ont_olt_context")
@patch("app.services.network.olt_ssh_service_ports.create_single_service_port")
def test_create_olt_sp_ssh_failure(mock_create: MagicMock, mock_resolve: MagicMock) -> None:
    db = MagicMock()
    mock_resolve.return_value = {
        "olt": MagicMock(),
        "fsp": "0/1/3",
        "olt_ont_id": 5,
    }
    mock_create.return_value = (False, "SSH timeout")

    result = execute_create_olt_service_port(
        db, {"ont_unit_id": "abc"}, {"vlan_id": 203}
    )
    assert result.status == "failed"
    assert "SSH timeout" in result.detail


# ── execute_ensure_nas_vlan ──


def test_ensure_nas_vlan_fails_without_device() -> None:
    db = MagicMock()
    result = execute_ensure_nas_vlan(db, {}, {})
    assert result.status == "failed"
    assert "NAS device" in result.detail


def test_ensure_nas_vlan_fails_without_vlan_id() -> None:
    db = MagicMock()
    result = execute_ensure_nas_vlan(
        db, {}, {"nas_device_id": "abc", "ip_address": "10.0.0.1/24"}
    )
    assert result.status == "failed"
    assert "VLAN ID" in result.detail


def test_ensure_nas_vlan_fails_without_ip() -> None:
    db = MagicMock()
    result = execute_ensure_nas_vlan(
        db, {}, {"nas_device_id": "abc", "vlan_id": 203}
    )
    assert result.status == "failed"
    assert "IP address" in result.detail


def test_ensure_nas_vlan_fails_device_not_found() -> None:
    db = MagicMock()
    db.get.return_value = None
    result = execute_ensure_nas_vlan(
        db, {}, {"nas_device_id": "abc", "vlan_id": 203, "ip_address": "10.0.0.1/24"}
    )
    assert result.status == "failed"
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
    assert result.payload["nas_vlan_provisioned"] is True
    mock_provision.assert_called_once()


# ── execute_push_tr069_wan_config ──


def test_push_wan_config_fails_without_device() -> None:
    db = MagicMock()
    result = execute_push_tr069_wan_config(db, {}, {})
    assert result.status == "failed"
    assert "ONT or CPE" in result.detail


# ── execute_push_tr069_pppoe_credentials ──


def test_push_pppoe_fails_without_credentials() -> None:
    db = MagicMock()
    db.scalars.return_value.first.return_value = None
    result = execute_push_tr069_pppoe_credentials(
        db, {"subscriber_id": "sub2"}, {}
    )
    assert result.status == "failed"
    assert "credentials not found" in result.detail


def test_push_pppoe_skips_cpe_devices() -> None:
    db = MagicMock()
    result = execute_push_tr069_pppoe_credentials(
        db, {"cpe_device_id": "cpe1"}, {"pppoe_username": "user", "pppoe_password": "pass"}
    )
    assert result.status == "ok"
    assert "CPE" in result.detail
    assert result.payload["tr069_pppoe_skipped_cpe"] is True


@patch("app.services.network.ont_action_network.set_pppoe_credentials")
def test_push_pppoe_delegates_to_ont_action(mock_set: MagicMock) -> None:
    from app.services.network.ont_action_common import ActionResult

    db = MagicMock()
    mock_set.return_value = ActionResult(success=True, message="PPPoE pushed")

    result = execute_push_tr069_pppoe_credentials(
        db,
        {"ont_unit_id": "ont1"},
        {"pppoe_username": "100025913", "pppoe_password": "secret123"},
    )
    assert result.status == "ok"
    # Verify username is NOT in the payload (security fix)
    assert "pppoe_username" not in (result.payload or {})
    mock_set.assert_called_once_with(db, "ont1", "100025913", "secret123")
