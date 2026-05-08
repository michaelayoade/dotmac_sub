from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock


def _patch_common(monkeypatch, *, adapter, values):
    from app.services.network import ont_provision_steps
    from app.services.network.ont_provisioning.context import OltContext

    ont = SimpleNamespace(id="ont-1", serial_number="HWTC12345678")
    olt = SimpleNamespace(id="olt-1", name="OLT 1")
    ctx = OltContext(ont=ont, olt=olt, fsp="0/2/11", olt_ont_id=13)

    monkeypatch.setattr(
        ont_provision_steps,
        "resolve_olt_context",
        lambda *_args, **_kwargs: (ctx, ""),
    )
    monkeypatch.setattr(
        ont_provision_steps,
        "resolve_effective_ont_config",
        lambda *_args, **_kwargs: {
            "config_pack": SimpleNamespace(),
            "values": values,
        },
    )
    monkeypatch.setattr(
        "app.services.network.olt_protocol_adapters.get_protocol_adapter",
        lambda *_args, **_kwargs: adapter,
    )
    monkeypatch.setattr(
        ont_provision_steps,
        "_validate_olt_profile_dependencies",
        lambda *_args, **_kwargs: None,
    )
    monkeypatch.setattr(ont_provision_steps, "_record_step", lambda *_args: None)
    monkeypatch.setattr(
        ont_provision_steps,
        "_send_failure_notification",
        lambda *_args: None,
    )
    monkeypatch.setattr(
        ont_provision_steps,
        "broadcast_websocket",
        lambda **_kwargs: None,
    )


def test_provision_bridge_configures_native_vlan(monkeypatch) -> None:
    from app.services.network.ont_provision_steps import provision_with_reconciliation

    adapter = MagicMock()
    adapter.create_service_port.return_value = SimpleNamespace(
        success=True,
        message="created",
        data={"service_port_index": 700},
    )
    adapter.configure_port_native_vlan.return_value = SimpleNamespace(
        success=True,
        message="native vlan set",
    )

    _patch_common(
        monkeypatch,
        adapter=adapter,
        values={
            "wan_vlan": 203,
            "wan_gem_index": 1,
            "wan_mode": "bridged",
            "mgmt_vlan": None,
        },
    )

    result = provision_with_reconciliation(MagicMock(), "ont-1")

    assert result.success is True
    adapter.create_service_port.assert_called_once_with(
        "0/2/11",
        13,
        gem_index=1,
        vlan_id=203,
    )
    adapter.configure_port_native_vlan.assert_called_once_with(
        "0/2/11",
        13,
        eth_port=1,
        vlan_id=203,
    )
    assert "native_vlan_203_eth_1" in result.data["steps_completed"]


def test_provision_bridge_native_vlan_failure_rolls_back_created_port(
    monkeypatch,
) -> None:
    from app.services.network.ont_provision_steps import provision_with_reconciliation

    adapter = MagicMock()
    adapter.create_service_port.return_value = SimpleNamespace(
        success=True,
        message="created",
        data={"service_port_index": 700},
    )
    adapter.configure_port_native_vlan.return_value = SimpleNamespace(
        success=False,
        message="OLT rejected native VLAN",
    )

    _patch_common(
        monkeypatch,
        adapter=adapter,
        values={
            "wan_vlan": 203,
            "wan_gem_index": 1,
            "wan_mode": "bridge",
            "mgmt_vlan": None,
        },
    )

    result = provision_with_reconciliation(MagicMock(), "ont-1")

    assert result.success is False
    assert "Bridge native VLAN failed" in result.message
    adapter.delete_service_port.assert_called_once_with(700)


def test_provision_fails_before_writes_when_dependency_audit_fails(monkeypatch) -> None:
    from app.services.network import ont_provision_steps
    from app.services.network.ont_provision_steps import (
        StepResult,
        provision_with_reconciliation,
    )

    adapter = MagicMock()

    _patch_common(
        monkeypatch,
        adapter=adapter,
        values={
            "wan_vlan": 203,
            "wan_gem_index": 1,
            "wan_mode": "bridge",
            "mgmt_vlan": None,
        },
    )
    monkeypatch.setattr(
        ont_provision_steps,
        "_validate_olt_profile_dependencies",
        lambda *_args, **_kwargs: StepResult(
            "provision",
            False,
            "OLT provisioning dependency audit failed: missing WAN config profile(s): 0",
        ),
    )

    result = provision_with_reconciliation(MagicMock(), "ont-1")

    assert result.success is False
    assert "dependency audit failed" in result.message
    adapter.create_service_port.assert_not_called()
    adapter.configure_port_native_vlan.assert_not_called()
