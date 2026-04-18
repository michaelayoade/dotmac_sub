from __future__ import annotations

from types import SimpleNamespace


def test_ipam_adapter_delegates_olt_assignments(monkeypatch) -> None:
    from app.services.ipam_adapter import ipam_adapter
    from app.services.network import olt_web_resources

    calls = {}

    def fake_assign_vlan(db, olt_id, vlan_id):
        calls["assign_vlan"] = (db, olt_id, vlan_id)
        return True, "assigned"

    def fake_assign_pool(db, olt_id, pool_id, vlan_id=None):
        calls["assign_pool"] = (db, olt_id, pool_id, vlan_id)
        return True, "assigned"

    monkeypatch.setattr(olt_web_resources, "assign_vlan_to_olt", fake_assign_vlan)
    monkeypatch.setattr(olt_web_resources, "assign_ip_pool_to_olt", fake_assign_pool)

    db = object()

    assert ipam_adapter.assign_vlan_to_olt(db, "olt-1", "vlan-1") == (
        True,
        "assigned",
    )
    assert ipam_adapter.assign_ip_pool_to_olt(db, "olt-1", "pool-1", "vlan-1") == (
        True,
        "assigned",
    )
    assert calls["assign_vlan"] == (db, "olt-1", "vlan-1")
    assert calls["assign_pool"] == (db, "olt-1", "pool-1", "vlan-1")


def test_olt_detail_adapter_builds_detail_context(monkeypatch) -> None:
    from app.services import (
        audit_helpers,
        web_network_core_devices,
        web_network_operations,
    )
    from app.services import olt_action_adapter as action_adapter_module
    from app.services.olt_detail_adapter import olt_detail_adapter

    calls = {}
    olt = SimpleNamespace(
        id="olt-1",
        ssh_username="admin",
        ssh_port=2222,
        ssh_password="secret",
        netconf_enabled=True,
        netconf_port=830,
        firmware_version="V1R1",
        software_version="SPH001",
        vendor="Huawei",
        tr069_acs_server=SimpleNamespace(
            cwmp_url="https://acs.example/cwmp",
            cwmp_username="cwmp-user",
        ),
    )
    monitoring_device = SimpleNamespace(
        snmp_enabled=True,
        snmp_port=161,
        snmp_version="v2c",
        snmp_community="private-community",
        snmp_username="snmp-user",
    )
    direct_and_assigned_ont = SimpleNamespace(
        id="ont-1",
        serial_number="ONT001",
        olt_device_id="olt-1",
        board="0/1",
        port="1",
    )
    direct_only_ont = SimpleNamespace(
        id="ont-2",
        serial_number="ONT002",
        olt_device_id="olt-1",
        board="0/1",
        port="2",
    )
    mismatch_ont = SimpleNamespace(
        id="ont-3",
        serial_number="ONT003",
        olt_device_id="foreign-olt",
        board="0/1",
        port="3",
    )
    assignment = SimpleNamespace(
        pon_port=SimpleNamespace(id="pon-1", olt_id="olt-1", name="0/1/1"),
    )
    mismatch_assignment = SimpleNamespace(
        pon_port=SimpleNamespace(id="pon-2", olt_id="olt-1", name="0/1/3"),
    )

    def fake_page_data(db, olt_id):
        calls["args"] = (db, olt_id)
        return {
            "olt": olt,
            "monitoring_device": monitoring_device,
            "adapter": "olt_detail",
            "olt_vlans": ["vlan-1"],
            "available_vlans": ["vlan-2"],
            "olt_ip_pool_usage": ["pool-1"],
            "available_ip_pools": ["pool-2"],
            "onts_on_olt": [direct_and_assigned_ont, direct_only_ont, mismatch_ont],
            "assignment_by_ont_id": {
                "ont-1": assignment,
                "ont-3": mismatch_assignment,
            },
            "signal_data": {"ont-1": {"status": "online"}},
            "pon_port_display_by_ont_id": {"ont-1": "0/1/1"},
            "ont_mac_by_ont_id": {"ont-1": "AA:BB:CC"},
        }

    def fake_operation_history(db, entity_type, entity_id):
        calls["operations"] = (db, entity_type, entity_id)
        return ["operation"]

    def fake_audit_activities(db, entity_type, entity_id):
        calls["activities"] = (db, entity_type, entity_id)
        return ["activity"]

    def fake_firmware_images(db, olt_id):
        calls["firmware"] = (db, olt_id)
        return ["firmware"]

    monkeypatch.setattr(web_network_core_devices, "olt_detail_page_data", fake_page_data)
    monkeypatch.setattr(
        web_network_operations, "build_operation_history", fake_operation_history
    )
    monkeypatch.setattr(audit_helpers, "build_audit_activities", fake_audit_activities)
    monkeypatch.setattr(
        action_adapter_module.olt_action_adapter,
        "get_olt_firmware_images",
        fake_firmware_images,
    )

    db = object()
    result = olt_detail_adapter.page_data(db, olt_id="olt-1")

    assert result["adapter"] == "olt_detail"
    assert calls["args"] == (db, "olt-1")
    assert calls["operations"] == (db, "olt", "olt-1")
    assert calls["activities"] == (db, "olt", "olt-1")
    assert calls["firmware"] == (db, "olt-1")
    assert result["activities"] == ["activity"]
    assert result["operations"] == ["operation"]
    assert result["available_olt_firmware"] == ["firmware"]
    assert result["acs_prefill"] == {
        "cwmp_url": "https://acs.example/cwmp",
        "cwmp_username": "cwmp-user",
    }
    assert result["detail_actions"]["sidebar"]["test_ssh"]["url"] == (
        "/admin/network/olts/olt-1/test-ssh"
    )
    assert result["terminal_context"]["actions"]["cli"] == (
        "/admin/network/olts/olt-1/cli"
    )
    assert "display version" in result["terminal_context"]["quick_commands"]
    assert result["firmware_context"]["current_version"] == "V1R1"
    assert result["config_context"]["vlans"] == ["vlan-1"]
    relationship_context = result["ont_relationship_context"]
    assert relationship_context["summary"] == {
        "total": 3,
        "direct_only": 1,
        "assignment_only": 0,
        "direct_and_assignment": 1,
        "mismatches": 1,
    }
    relationship_rows = relationship_context["rows"]
    assert relationship_rows[0]["relationship_source"] == "direct+assignment"
    assert relationship_rows[0]["port_display"] == "0/1/1"
    assert relationship_rows[1]["relationship_source"] == "direct"
    assert relationship_rows[2]["relationship_mismatch"] is True

    access_info = result["access_info"]
    assert access_info["ssh"]["password_status"] == "Saved"
    assert access_info["snmp"]["credential_label"] == "Community"
    assert access_info["snmp"]["credential_status"] == "Saved"
    assert "private-community" not in repr(access_info)
    assert "snmp-user" not in repr(access_info)


def test_olt_action_adapter_delegates_find_ont_and_running_config(monkeypatch) -> None:
    from app.services.network import olt_operations
    from app.services.olt_action_adapter import olt_action_adapter

    calls = {}

    def fake_fetch_running_config(olt, db=None):
        calls["running_config"] = (olt, db)
        return "display current-configuration"

    def fake_get_ont_status_by_serial(db, olt_id, serial, **kwargs):
        calls["find_ont"] = (db, olt_id, serial, kwargs)
        return True, "found", {"serial": serial}

    monkeypatch.setattr(olt_operations, "fetch_running_config", fake_fetch_running_config)
    monkeypatch.setattr(
        olt_operations,
        "get_ont_status_by_serial",
        fake_get_ont_status_by_serial,
    )

    db = object()
    olt = object()

    assert olt_action_adapter.fetch_running_config(olt, db=db) == (
        "display current-configuration"
    )
    ok, message, payload = olt_action_adapter.get_ont_status_by_serial(
        db, "olt-1", "HWTC1234", request="request-context"
    )

    assert ok is True
    assert message == "found"
    assert payload == {"serial": "HWTC1234"}
    assert calls["running_config"] == (olt, db)
    assert calls["find_ont"] == (
        db,
        "olt-1",
        "HWTC1234",
        {"request": "request-context"},
    )


def test_olt_action_adapter_delegates_authorization_queue(monkeypatch) -> None:
    from app.services.network import olt_authorization_workflow
    from app.services.olt_action_adapter import olt_action_adapter

    calls = {}

    def fake_queue(db, **kwargs):
        calls["queue"] = (db, kwargs)
        return True, "queued", "op-1"

    monkeypatch.setattr(
        olt_authorization_workflow,
        "queue_authorize_autofind_ont",
        fake_queue,
    )

    db = object()
    result = olt_action_adapter.queue_authorize_autofind_ont(
        db,
        olt_id="olt-1",
        fsp="0/1/1",
        serial_number="HWTC1234",
        force_reauthorize=True,
        initiated_by="admin",
        request="request-context",
    )

    assert result == (True, "queued", "op-1")
    assert calls["queue"] == (
        db,
        {
            "olt_id": "olt-1",
            "fsp": "0/1/1",
            "serial_number": "HWTC1234",
            "force_reauthorize": True,
            "initiated_by": "admin",
            "request": "request-context",
        },
    )
