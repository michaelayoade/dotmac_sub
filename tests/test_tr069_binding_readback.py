from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock


def test_parse_tr069_binding_for_target_ont() -> None:
    from app.services.network.olt_ssh_ont.tr069 import parse_tr069_binding

    output = """
    ont tr069-server-config 0 0 profile-id 2
    ont tr069-server-config 1 13 profile-id 5
    """

    assert parse_tr069_binding(output, port=1, ont_id=13) == 5
    assert parse_tr069_binding(output, port=1, ont_id=14) is None


def test_provision_fails_when_tr069_binding_readback_mismatches(monkeypatch) -> None:
    from app.services.network import ont_provision_steps
    from app.services.network.ont_provision_steps import provision_with_reconciliation
    from app.services.network.ont_provisioning.context import OltContext

    ont = SimpleNamespace(id="ont-1", serial_number="RTKG00060198")
    olt = SimpleNamespace(id="olt-1", name="Garki Huawei OLT")
    ctx = OltContext(ont=ont, olt=olt, fsp="0/2/1", olt_ont_id=13)
    config_pack = SimpleNamespace(
        management_vlan=SimpleNamespace(tag=201),
        internet_config_ip_index=1,
        wan_config_profile_id=0,
        tr069_olt_profile_id=5,
    )
    values = {
        "wan_vlan": 203,
        "wan_gem_index": 1,
        "wan_mode": "pppoe",
        "mgmt_vlan": 201,
        "mgmt_gem_index": 2,
        "mgmt_ip_mode": "static_ip",
        "mgmt_ip_address": "172.16.201.141",
        "mgmt_subnet": "255.255.255.0",
        "mgmt_gateway": "172.16.201.1",
        "tr069_olt_profile_id": 5,
        "internet_config_ip_index": 1,
        "wan_config_profile_id": 0,
    }

    adapter = MagicMock()
    adapter.create_service_port.return_value = SimpleNamespace(
        success=True,
        message="created",
        data={},
    )
    adapter.clear_iphost_config.return_value = SimpleNamespace(success=False)
    adapter.clear_internet_config.return_value = SimpleNamespace(success=False)
    adapter.clear_wan_config.return_value = SimpleNamespace(success=False)
    adapter.configure_management_batch.return_value = SimpleNamespace(
        success=True,
        message="management configured",
        data={
            "steps_completed": [
                "create_mgmt_service_port",
                "configure_iphost",
                "bind_tr069",
            ]
        },
    )
    adapter.get_service_ports_for_ont.return_value = SimpleNamespace(
        success=True,
        message="ports ok",
        data={"service_ports": []},
    )
    adapter.get_tr069_profile_binding.return_value = SimpleNamespace(
        success=True,
        message="No TR-069 profile binding found for ONT 13 on 0/2/1",
        data={"profile_id": None},
    )

    monkeypatch.setattr(
        ont_provision_steps,
        "resolve_olt_context",
        lambda *_args, **_kwargs: (ctx, ""),
    )
    monkeypatch.setattr(
        ont_provision_steps,
        "resolve_effective_ont_config",
        lambda *_args, **_kwargs: {"config_pack": config_pack, "values": values},
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
    monkeypatch.setattr(ont_provision_steps, "_record_step", lambda *_args: None)
    monkeypatch.setattr(
        ont_provision_steps, "_send_failure_notification", lambda *_args: None
    )

    result = provision_with_reconciliation(MagicMock(), "ont-1")

    assert result.success is False
    assert "TR-069 profile binding readback failed" in result.message
    assert result.data["expected_tr069_profile_id"] == 5
    assert result.data["readback_tr069_profile_id"] is None
