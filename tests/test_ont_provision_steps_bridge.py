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
    timings = result.data["command_timings"]
    assert [timing["command"] for timing in timings[:2]] == [
        "create_internet_service_port",
        "configure_bridge_native_vlan",
    ]
    assert all(isinstance(timing["duration_ms"], int) for timing in timings)
    assert all("message" not in timing for timing in timings)
    assert [phase["phase"] for phase in result.data["phase_timings"]] == [
        "prepare",
        "internet_l2",
        "stale_wan_cleanup",
        "management_and_omci_apply",
        "service_port_readback",
        "tr069_binding_readback",
    ]


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
    assert result.data["command_timings"][-1]["command"] == (
        "rollback_internet_service_port"
    )


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


def test_provision_reserves_olt_pool_management_ip_before_iphost(
    monkeypatch,
) -> None:
    from app.services.network import ont_provision_steps
    from app.services.network.ont_provision_steps import provision_with_reconciliation

    adapter = MagicMock()
    adapter.create_service_port.return_value = SimpleNamespace(
        success=True,
        message="created",
        data={"service_port_index": 700},
    )
    adapter.clear_internet_config.return_value = SimpleNamespace(success=False)
    adapter.clear_wan_config.return_value = SimpleNamespace(success=False)
    adapter.configure_management_batch.return_value = SimpleNamespace(
        success=True,
        message="management configured",
        data={"steps_completed": ["bind_tr069"]},
    )
    adapter.get_tr069_profile_binding.return_value = SimpleNamespace(
        success=True,
        message="TR-069 profile 5 bound",
        data={"profile_id": 5},
    )

    first_values = {
        "wan_vlan": 203,
        "wan_gem_index": 1,
        "wan_mode": "pppoe",
        "mgmt_vlan": 201,
        "mgmt_gem_index": 2,
        "tr069_olt_profile_id": 5,
        "internet_config_ip_index": 1,
        "wan_config_profile_id": 0,
    }
    second_values = {
        **first_values,
        "mgmt_ip_mode": "static_ip",
        "mgmt_ip_address": "172.16.201.137",
        "mgmt_subnet": "255.255.255.0",
        "mgmt_gateway": "172.16.201.1",
    }

    from app.services.network.ont_provisioning.context import OltContext

    ont = SimpleNamespace(id="ont-1", serial_number="HWTC12345678")
    olt = SimpleNamespace(id="olt-1", name="OLT 1", mgmt_ip_pool_id="pool-1")
    ctx = OltContext(ont=ont, olt=olt, fsp="0/2/11", olt_ont_id=13)
    config_pack = SimpleNamespace(
        management_vlan=SimpleNamespace(tag=201),
        internet_config_ip_index=1,
        wan_config_profile_id=0,
        tr069_olt_profile_id=5,
    )
    configs = [
        {"config_pack": config_pack, "values": first_values},
        {"config_pack": config_pack, "values": second_values},
    ]

    monkeypatch.setattr(
        ont_provision_steps,
        "resolve_olt_context",
        lambda *_args, **_kwargs: (ctx, ""),
    )
    monkeypatch.setattr(
        ont_provision_steps,
        "resolve_effective_ont_config",
        lambda *_args, **_kwargs: configs.pop(0),
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
    monkeypatch.setattr(
        "app.services.network.iphost_priority.resolve_management_iphost_priority",
        lambda *_args, **_kwargs: 2,
    )
    allocation = SimpleNamespace(address="172.16.201.137", reused=False)
    allocate_calls = []

    def fake_allocate(db, *, ont, olt, pool_id):
        allocate_calls.append((ont, olt, pool_id))
        return allocation

    monkeypatch.setattr(
        "app.services.network.ont_management_ipam.allocate_ont_management_ip",
        fake_allocate,
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

    result = provision_with_reconciliation(MagicMock(), "ont-1")

    assert result.success is True
    assert allocate_calls == [(ont, olt, "pool-1")]
    spec = adapter.configure_management_batch.call_args.args[0]
    assert spec.ip_mode == "static"
    assert spec.ip_address == "172.16.201.137"
    assert spec.subnet_mask == "255.255.255.0"
    assert spec.gateway == "172.16.201.1"
    assert "reserved_management_ip" in result.data["steps_completed"]
