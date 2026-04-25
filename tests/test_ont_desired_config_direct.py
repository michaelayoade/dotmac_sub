"""Coverage for direct ONT desired-config provisioning behavior."""

from __future__ import annotations


def test_desired_config_setter_maps_legacy_fields_without_override_rows(db_session):
    from app.models.network import OntUnit
    from app.services.network.ont_desired_config import upsert_ont_desired_config_value

    ont = OntUnit(serial_number="DESIRED-CFG-001")
    db_session.add(ont)
    db_session.flush()

    upsert_ont_desired_config_value(
        db_session,
        ont=ont,
        field_name="wifi.ssid",
        value="Dotmac-WiFi",
    )
    upsert_ont_desired_config_value(
        db_session,
        ont=ont,
        field_name="management.ip_address",
        value="192.0.2.10",
    )
    upsert_ont_desired_config_value(
        db_session,
        ont=ont,
        field_name="authorization.line_profile_id",
        value=10,
    )

    assert ont.desired_config == {
        "wifi": {"ssid": "Dotmac-WiFi"},
        "management": {"ip_address": "192.0.2.10"},
        "authorization": {"line_profile_id": 10},
    }


def test_effective_config_merges_olt_defaults_and_ont_desired_config(db_session):
    from app.models.catalog import RegionZone
    from app.models.network import OLTDevice, OntUnit, Vlan, VlanPurpose
    from app.services.network.effective_ont_config import resolve_effective_ont_config

    region = RegionZone(name="Test Region", code="desired-config")
    olt = OLTDevice(
        name="OLT Defaults",
        default_line_profile_id=10,
        default_service_profile_id=20,
        default_tr069_olt_profile_id=30,
    )
    db_session.add_all([region, olt])
    db_session.flush()

    internet_vlan = Vlan(
        region_id=region.id,
        olt_device_id=olt.id,
        name="Internet",
        tag=100,
        purpose=VlanPurpose.internet,
    )
    mgmt_vlan = Vlan(
        region_id=region.id,
        olt_device_id=olt.id,
        name="Management",
        tag=200,
        purpose=VlanPurpose.management,
    )
    db_session.add_all([internet_vlan, mgmt_vlan])
    db_session.flush()
    olt.internet_vlan_id = internet_vlan.id
    olt.management_vlan_id = mgmt_vlan.id

    ont = OntUnit(
        serial_number="DESIRED-CFG-002",
        olt_device_id=olt.id,
        desired_config={
            "wan": {"pppoe_username": "subscriber@example"},
            "wifi": {"ssid": "Subscriber-WiFi"},
            "management": {"ip_address": "192.0.2.20"},
        },
    )
    db_session.add(ont)
    db_session.flush()

    values = resolve_effective_ont_config(db_session, ont)["values"]

    assert values["wan_vlan"] == 100
    assert values["mgmt_vlan"] == 200
    assert values["authorization_line_profile_id"] == 10
    assert values["authorization_service_profile_id"] == 20
    assert values["tr069_olt_profile_id"] == 30
    assert values["pppoe_username"] == "subscriber@example"
    assert values["wifi_ssid"] == "Subscriber-WiFi"
    assert values["mgmt_ip_address"] == "192.0.2.20"


def test_effective_config_ignores_legacy_ont_flat_config_fields(db_session):
    from app.models.network import OnuMode, OntUnit
    from app.models.tr069 import Tr069AcsServer
    from app.services.network.effective_ont_config import resolve_effective_ont_config

    acs = Tr069AcsServer(
        name="Ignored Flat ACS",
        base_url="http://ignored-flat-acs.example",
        is_active=True,
    )
    db_session.add(acs)
    db_session.flush()

    ont = OntUnit(
        serial_number="DESIRED-CFG-FLAT-IGNORED",
        onu_mode=OnuMode.routing,
        tr069_acs_server_id=acs.id,
        tr069_olt_profile_id=99,
        desired_config={},
    )
    db_session.add(ont)
    db_session.flush()

    values = resolve_effective_ont_config(db_session, ont)["values"]

    assert values["onu_mode"] is None
    assert values["tr069_acs_server_id"] is None
    assert values["tr069_olt_profile_id"] is None


def test_desired_state_does_not_default_management_ip_mode_from_olt_vlan(
    db_session, monkeypatch
):
    from types import SimpleNamespace

    from app.models.catalog import RegionZone
    from app.models.network import OLTDevice, OntUnit, Vlan, VlanPurpose
    from app.services.network.ont_provisioning.state import (
        build_desired_state_from_config,
    )

    region = RegionZone(name="Mgmt Region", code="mgmt-no-default")
    olt = OLTDevice(name="Mgmt OLT")
    db_session.add_all([region, olt])
    db_session.flush()
    mgmt_vlan = Vlan(
        region_id=region.id,
        olt_device_id=olt.id,
        name="Management",
        tag=200,
        purpose=VlanPurpose.management,
    )
    db_session.add(mgmt_vlan)
    db_session.flush()
    olt.management_vlan_id = mgmt_vlan.id

    ont = OntUnit(
        serial_number="DESIRED-CFG-MGMT-NO-DEFAULT",
        olt_device_id=olt.id,
        desired_config={},
    )
    db_session.add(ont)
    db_session.flush()

    monkeypatch.setattr(
        "app.services.network.ont_provisioning.context.resolve_olt_context",
        lambda db, ont_id: (SimpleNamespace(fsp="0/1/1", olt_ont_id=1), None),
    )

    desired, err = build_desired_state_from_config(db_session, str(ont.id))

    assert err == ""
    assert desired is not None
    assert desired.management is None


def test_direct_orchestrator_stops_after_reconciliation_failure(db_session, monkeypatch):
    from app.services.network.ont_provisioning.orchestrator import (
        provision_ont_from_desired_config,
    )
    from app.services.network.ont_provisioning.result import StepResult

    calls: list[str] = []

    def fake_provision(*args, **kwargs):
        calls.append("provision")
        return StepResult("provision_reconciled", False, "missing OLT context")

    monkeypatch.setattr(
        "app.services.network.ont_provision_steps.provision_with_reconciliation",
        fake_provision,
    )
    monkeypatch.setattr(
        "app.services.network.ont_provision_steps.wait_tr069_bootstrap",
        lambda *args, **kwargs: calls.append("wait"),
    )
    monkeypatch.setattr(
        "app.services.network.ont_provision_steps.apply_saved_service_config",
        lambda *args, **kwargs: calls.append("apply"),
    )

    result = provision_ont_from_desired_config(db_session, "missing-ont")

    assert result.success is False
    assert result.failed_step == "provision_reconciled"
    assert calls == ["provision"]


def test_direct_orchestrator_skips_acs_when_tr069_not_configured(
    db_session, monkeypatch
):
    from app.models.network import OntUnit
    from app.services.network.ont_provisioning.orchestrator import (
        provision_ont_from_desired_config,
    )
    from app.services.network.ont_provisioning.result import StepResult

    ont = OntUnit(serial_number="DESIRED-CFG-003", desired_config={"wan": {"mode": "dhcp"}})
    db_session.add(ont)
    db_session.flush()

    calls: list[str] = []

    monkeypatch.setattr(
        "app.services.network.ont_provision_steps.provision_with_reconciliation",
        lambda *args, **kwargs: calls.append("provision")
        or StepResult("provision_reconciled", True, "ok"),
    )
    monkeypatch.setattr(
        "app.services.network.ont_provision_steps.wait_tr069_bootstrap",
        lambda *args, **kwargs: calls.append("wait"),
    )
    monkeypatch.setattr(
        "app.services.network.ont_provision_steps.apply_saved_service_config",
        lambda *args, **kwargs: calls.append("apply"),
    )

    result = provision_ont_from_desired_config(db_session, str(ont.id))

    assert result.success is True
    assert calls == ["provision"]


def test_apply_saved_service_config_uses_desired_config_for_pppoe(
    db_session, monkeypatch
):
    from types import SimpleNamespace

    from app.models.network import OntUnit
    from app.services.network.ont_provision_steps import apply_saved_service_config

    ont = OntUnit(
        serial_number="DESIRED-CFG-004",
        desired_config={
            "tr069": {
                "acs_server_id": "acs-1",
                "olt_profile_id": 30,
                "cr_username": "cr-user",
                "cr_password": "cr-pass",
            },
            "wan": {
                "mode": "pppoe",
                "vlan": 100,
                "pppoe_username": "subscriber@example",
                "pppoe_password": "secret",
            },
        },
    )
    db_session.add(ont)
    db_session.flush()

    calls: dict[str, object] = {}

    monkeypatch.setattr(
        "app.services.network.ont_provision_steps.get_pppoe_provisioning_method",
        lambda db: "tr069",
    )
    monkeypatch.setattr(
        "app.services.network.ont_action_network.probe_wan_capabilities",
        lambda db, ont_id: SimpleNamespace(success=True, message="probed"),
    )

    def fake_set_pppoe(db, ont_id, **kwargs):
        calls["pppoe"] = kwargs
        return SimpleNamespace(success=True, message="pppoe pushed", waiting=False)

    monkeypatch.setattr(
        "app.services.network.ont_action_wan.set_pppoe_credentials",
        fake_set_pppoe,
    )
    monkeypatch.setattr(
        "app.services.network.ont_provision_steps._acs_config_writer",
        lambda: SimpleNamespace(
            set_connection_request_credentials=lambda *args, **kwargs: SimpleNamespace(
                success=True,
                message="credentials pushed",
            )
        ),
    )

    result = apply_saved_service_config(db_session, str(ont.id))

    assert result.success is True
    assert calls["pppoe"] == {
        "username": "subscriber@example",
        "password": "secret",
        "instance_index": 1,
        "ensure_instance": True,
        "wan_vlan": 100,
    }


def test_apply_saved_service_config_skips_wan_when_wan_mode_absent(
    db_session, monkeypatch
):
    from app.models.network import OntUnit
    from app.services.network.ont_provision_steps import apply_saved_service_config

    ont = OntUnit(
        serial_number="DESIRED-CFG-ACS-ONLY",
        desired_config={"tr069": {"acs_server_id": "acs-1", "olt_profile_id": 30}},
    )
    db_session.add(ont)
    db_session.flush()

    def fail_probe(*args, **kwargs):
        raise AssertionError("WAN probing should not run without desired_config.wan.mode")

    monkeypatch.setattr(
        "app.services.network.ont_action_network.probe_wan_capabilities",
        fail_probe,
    )

    result = apply_saved_service_config(db_session, str(ont.id))

    assert result.success is False
    assert result.message == "Saved ONT service config is incomplete."
    assert result.data["needs_input"] == [
        "Connection request credentials are incomplete in desired_config/OLT defaults."
    ]
    assert "probe_wan_capabilities" not in {
        step["step"] for step in result.data.get("steps", [])
    }


def test_provision_wizard_context_does_not_invent_missing_desired_config_defaults(
    db_session, monkeypatch
):
    from types import SimpleNamespace

    from app.models.network import OntUnit
    from app.services import web_network_onts

    ont = OntUnit(serial_number="DESIRED-CFG-WIZARD", desired_config={})
    db_session.add(ont)
    db_session.flush()

    monkeypatch.setattr(
        "app.services.network.ont_units.get_including_inactive",
        lambda **kwargs: ont,
    )
    monkeypatch.setattr(
        "app.services.web_admin.get_current_user",
        lambda request: SimpleNamespace(id="user-1"),
    )
    monkeypatch.setattr(
        "app.services.web_admin.get_sidebar_stats",
        lambda db: {},
    )
    monkeypatch.setattr(
        web_network_onts,
        "get_tr069_profiles_for_ont",
        lambda db, ont: ([], None),
    )
    monkeypatch.setattr(web_network_onts, "get_vlans_for_ont", lambda db, ont: [])
    monkeypatch.setattr(web_network_onts, "get_tr069_servers", lambda db: [])
    monkeypatch.setattr(web_network_onts, "get_speed_profiles", lambda db, direction: [])

    context = web_network_onts.provision_wizard_context(
        SimpleNamespace(),
        db_session,
        str(ont.id),
    )

    assert "Select management IP method" in context["provision_gate_issues"]
    assert "Select internet deployment method" in context["provision_gate_issues"]
    assert "Enter PPPoE username" not in context["provision_gate_issues"]
    assert context["pppoe_username"] is None


def test_save_provision_settings_persists_tr069_profile_to_desired_config(
    db_session, monkeypatch
):
    from app.models.network import OntUnit
    from app.services.web_network_onts_provisioning import save_provision_settings

    ont = OntUnit(serial_number="DESIRED-CFG-TR069-SAVE", desired_config={})
    db_session.add(ont)
    db_session.flush()

    monkeypatch.setattr(
        "app.services.network.ont_units.get_including_inactive",
        lambda **kwargs: ont,
    )
    monkeypatch.setattr(
        "app.services.web_network_onts_provisioning.update_service_order_execution_context_for_ont",
        lambda *args, **kwargs: None,
    )

    result = save_provision_settings(
        db_session,
        ont_id=str(ont.id),
        tr069_profile_id="42",
        onu_mode=None,
        mgmt_vlan_id=None,
        mgmt_ip_mode=None,
        mgmt_ip_address=None,
        mgmt_subnet=None,
        mgmt_gateway=None,
        wan_protocol=None,
        wan_vlan_id=None,
        ip_pool_id=None,
        static_ip_pool_id=None,
        static_ip=None,
        static_subnet=None,
        static_gateway=None,
        static_dns=None,
        lan_ip=None,
        lan_subnet=None,
        dhcp_enabled=None,
        dhcp_start=None,
        dhcp_end=None,
        wifi_enabled=None,
        wifi_ssid=None,
        wifi_password=None,
        wifi_security_mode=None,
        wifi_channel=None,
        pppoe_username=None,
        pppoe_password=None,
    )

    assert result.status_code == 200
    assert ont.desired_config["tr069"]["olt_profile_id"] == 42


def test_manual_step_bind_tr069_persists_profile_to_desired_config(
    db_session, monkeypatch
):
    from types import SimpleNamespace

    from app.models.network import OntUnit
    from app.services.network.ont_provisioning.result import StepResult
    from app.web.admin import network_onts_provisioning

    ont = OntUnit(serial_number="DESIRED-CFG-TR069-BIND", desired_config={})
    db_session.add(ont)
    db_session.flush()

    monkeypatch.setattr(
        "app.services.network.ont_provision_steps.bind_tr069",
        lambda *args, **kwargs: StepResult("bind_tr069", True, "bound"),
    )
    monkeypatch.setattr(
        network_onts_provisioning,
        "_update_service_order_execution_context_for_ont",
        lambda *args, **kwargs: None,
    )
    monkeypatch.setattr(
        network_onts_provisioning,
        "_record_ont_step_action",
        lambda *args, **kwargs: None,
    )

    response = network_onts_provisioning.step_bind_tr069(
        SimpleNamespace(headers={}),
        str(ont.id),
        tr069_olt_profile_id=77,
        db=db_session,
    )

    assert response.status_code == 200
    assert ont.desired_config["tr069"]["olt_profile_id"] == 77


def test_direct_provision_route_ignores_posted_tr069_profile_override(
    db_session, monkeypatch
):
    import inspect
    from types import SimpleNamespace

    from app.web.admin import network_onts_provisioning

    captured: dict[str, object] = {}
    signature = inspect.signature(network_onts_provisioning.provision_ont_direct)
    assert "tr069_olt_profile_id" not in signature.parameters

    monkeypatch.setattr(
        "app.services.network.action_logging.actor_label",
        lambda request: "admin",
    )

    def fake_provision(db, ont_id, **kwargs):
        captured.update(kwargs)
        return SimpleNamespace(
            success=True,
            message="ok",
            duration_ms=1,
            steps=[],
            failed_step=None,
            to_dict=lambda: {"success": True},
        )

    monkeypatch.setattr(
        "app.services.network.ont_provisioning.orchestrator.provision_ont_from_desired_config",
        fake_provision,
    )
    monkeypatch.setattr(
        network_onts_provisioning,
        "log_network_action_result",
        lambda *args, **kwargs: None,
    )

    response = network_onts_provisioning.provision_ont_direct(
        SimpleNamespace(headers={}),
        "ont-1",
        async_execution=False,
        db=db_session,
    )

    assert response.status_code == 200
    assert "tr069_olt_profile_id" not in captured


def test_provisioning_entrypoints_do_not_accept_tr069_profile_override():
    import inspect

    from app.services.network.bulk_provisioning import bulk_provision_onts
    from app.services.network.ont_provisioning.orchestrator import (
        provision_ont_from_desired_config,
    )
    from app.services.network.ont_provisioning.preflight import validate_prerequisites
    from app.services.network.ont_provisioning.reconciler import reconcile_ont_state
    from app.services.network.ont_provisioning.state import (
        build_desired_state_from_config,
    )
    from app.services.network.ont_provision_steps import provision_with_reconciliation
    from app.tasks.ont_provisioning import provision_ont, queue_bulk_provisioning

    assert "tr069_olt_profile_id" not in inspect.signature(
        provision_ont_from_desired_config
    ).parameters
    assert "tr069_olt_profile_id" not in inspect.signature(provision_ont.run).parameters
    assert "tr069_olt_profile_id" not in inspect.signature(
        queue_bulk_provisioning.run
    ).parameters
    assert "tr069_olt_profile_id" not in inspect.signature(
        bulk_provision_onts
    ).parameters
    assert "tr069_olt_profile_id" not in inspect.signature(
        validate_prerequisites
    ).parameters
    assert "tr069_olt_profile_id" not in inspect.signature(
        build_desired_state_from_config
    ).parameters
    assert "tr069_olt_profile_id" not in inspect.signature(
        reconcile_ont_state
    ).parameters
    assert "tr069_olt_profile_id" not in inspect.signature(
        provision_with_reconciliation
    ).parameters
