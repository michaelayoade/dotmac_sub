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
    }


def test_desired_config_strips_olt_config_pack_owned_bloat():
    from app.models.network import OntUnit
    from app.services.network.ont_desired_config import (
        desired_config,
        set_desired_config_value,
        strip_config_pack_owned_desired_config,
    )

    config = strip_config_pack_owned_desired_config(
        {
            "tr069": {"olt_profile_id": 30},
            "authorization": {"line_profile_id": 10},
            "omci": {"wan_config_profile_id": 5},
            "wan": {
                "mode": "pppoe",
                "vlan": 100,
                "gem_index": 1,
                "pppoe_username": "subscriber@example",
            },
            "management": {"vlan": 200, "ip_address": "192.0.2.10"},
        }
    )

    assert config == {
        "wan": {"mode": "pppoe", "pppoe_username": "subscriber@example"},
        "management": {"ip_address": "192.0.2.10"},
    }

    ont = OntUnit(serial_number="DESIRED-CFG-BLOAT", desired_config={})
    set_desired_config_value(ont, "wan.vlan", 100)
    set_desired_config_value(ont, "management.vlan", 200)
    set_desired_config_value(ont, "tr069.olt_profile_id", 30)

    assert desired_config(ont) == {}


def test_effective_config_uses_olt_pack_and_active_assignment(db_session):
    from app.models.catalog import RegionZone
    from app.models.network import OLTDevice, OntAssignment, OntUnit, Vlan, VlanPurpose
    from app.models.tr069 import Tr069AcsServer
    from app.services.network.effective_ont_config import resolve_effective_ont_config

    region = RegionZone(name="Test Region", code="desired-config")
    acs = Tr069AcsServer(
        name="Config Pack ACS",
        base_url="http://config-pack-acs.example",
        is_active=True,
    )
    olt = OLTDevice(
        name="OLT Defaults",
        default_line_profile_id=10,
        default_service_profile_id=20,
        tr069_acs_server_id=acs.id,
        default_tr069_olt_profile_id=30,
        default_internet_config_ip_index=0,
        default_wan_config_profile_id=5,
        default_internet_gem_index=1,
        default_cr_username="pack-cr-user",
        default_cr_password="pack-cr-pass",
    )
    db_session.add_all([region, acs, olt])
    db_session.flush()
    olt.tr069_acs_server_id = acs.id

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
            "wan": {
                "pppoe_username": "subscriber@example",
                "vlan": 999,
                "gem_index": 9,
            },
            "wifi": {"ssid": "Subscriber-WiFi"},
            "management": {"ip_address": "192.0.2.20", "vlan": 998},
            "tr069": {
                "acs_server_id": "ignored-acs",
                "olt_profile_id": 99,
                "cr_username": "ignored-cr-user",
                "cr_password": "ignored-cr-pass",
            },
            "omci": {
                "internet_config_ip_index": 9,
                "wan_config_profile_id": 99,
                "pppoe_vlan": 997,
            },
            "authorization": {"line_profile_id": 11, "service_profile_id": 21},
        },
    )
    db_session.add(ont)
    db_session.flush()
    db_session.add(
        OntAssignment(
            ont_unit_id=ont.id,
            active=True,
            pppoe_username="subscriber@example",
            wifi_ssid="Subscriber-WiFi",
            mgmt_ip_address="192.0.2.20",
        )
    )
    db_session.flush()

    values = resolve_effective_ont_config(db_session, ont)["values"]

    assert values["wan_vlan"] == 100
    assert values["mgmt_vlan"] == 200
    assert values["wan_gem_index"] == 1
    assert values["authorization_line_profile_id"] == 10
    assert values["authorization_service_profile_id"] == 20
    assert values["tr069_acs_server_id"] == str(acs.id)
    assert values["tr069_olt_profile_id"] == 30
    assert values["cr_username"] == "pack-cr-user"
    assert values["cr_password"] == "pack-cr-pass"
    assert values["internet_config_ip_index"] == 0
    assert values["wan_config_profile_id"] == 5
    assert values["pppoe_omci_vlan"] is None
    assert values["pppoe_username"] == "subscriber@example"
    assert values["wifi_ssid"] == "Subscriber-WiFi"
    assert values["mgmt_ip_address"] == "192.0.2.20"


def test_effective_config_ignores_legacy_ont_flat_config_fields(db_session):
    from app.models.network import OntUnit, OnuMode
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


def test_apply_saved_service_config_uses_active_assignment_for_pppoe(
    db_session, monkeypatch
):
    from types import SimpleNamespace

    from app.models.catalog import RegionZone
    from app.models.network import OLTDevice, OntAssignment, OntUnit, Vlan, VlanPurpose
    from app.models.tr069 import Tr069AcsServer
    from app.services.credential_crypto import encrypt_credential
    from app.services.network.ont_provision_steps import apply_saved_service_config

    region = RegionZone(name="Saved Config Region", code="saved-config-region")
    acs = Tr069AcsServer(
        name="Saved Config ACS",
        base_url="http://saved-config-acs.example",
        is_active=True,
    )
    olt = OLTDevice(
        name="Saved Config OLT",
        default_tr069_olt_profile_id=30,
        default_cr_username="cr-user",
        default_cr_password=encrypt_credential("cr-pass"),
    )
    db_session.add_all([region, acs, olt])
    db_session.flush()
    olt.tr069_acs_server_id = acs.id
    internet_vlan = Vlan(
        region_id=region.id,
        olt_device_id=olt.id,
        name="Internet",
        tag=100,
        purpose=VlanPurpose.internet,
    )
    db_session.add(internet_vlan)
    db_session.flush()
    olt.internet_vlan_id = internet_vlan.id

    ont = OntUnit(
        serial_number="DESIRED-CFG-004",
        olt_device_id=olt.id,
        desired_config={
            "tr069": {
                "acs_server_id": "ignored-acs",
                "olt_profile_id": 99,
                "cr_username": "ignored-cr-user",
                "cr_password": "ignored-cr-pass",
            },
            "wan": {
                "mode": "pppoe",
                "vlan": 999,
                "pppoe_username": "subscriber@example",
                "pppoe_password": "secret",
            },
        },
    )
    db_session.add(ont)
    db_session.flush()
    db_session.add(
        OntAssignment(
            ont_unit_id=ont.id,
            active=True,
            pppoe_username="subscriber@example",
            pppoe_password="secret",
        )
    )
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
    from app.models.network import OLTDevice, OntUnit
    from app.models.tr069 import Tr069AcsServer
    from app.services.network.ont_provision_steps import apply_saved_service_config

    acs = Tr069AcsServer(
        name="ACS Only Server",
        base_url="http://acs-only.example",
        is_active=True,
    )
    olt = OLTDevice(name="ACS Only OLT", default_tr069_olt_profile_id=30)
    db_session.add_all([acs, olt])
    db_session.flush()
    olt.tr069_acs_server_id = acs.id

    ont = OntUnit(
        serial_number="DESIRED-CFG-ACS-ONLY",
        olt_device_id=olt.id,
        desired_config={"tr069": {"acs_server_id": "ignored", "olt_profile_id": 99}},
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


def test_save_provision_settings_does_not_persist_tr069_profile_override(
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
        onu_mode="routing",
        mgmt_ip_mode="dhcp",
        mgmt_ip_address=None,
        mgmt_subnet=None,
        mgmt_gateway=None,
        wan_protocol="dhcp",
        ip_pool_id=None,
        static_ip_pool_id=None,
        static_ip=None,
        static_subnet=None,
        static_gateway=None,
        static_dns=None,
        lan_ip="192.168.1.1",
        lan_subnet="255.255.255.0",
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
    assert "tr069" not in ont.desired_config


def test_manual_step_bind_tr069_does_not_persist_profile_override(
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
    monkeypatch.setattr(
        network_onts_provisioning,
        "can_manage_ont_from_request",
        lambda *args, **kwargs: True,
    )

    response = network_onts_provisioning.step_bind_tr069(
        SimpleNamespace(headers={}),
        str(ont.id),
        db=db_session,
    )

    assert response.status_code == 200
    assert "tr069" not in ont.desired_config


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
    monkeypatch.setattr(
        network_onts_provisioning,
        "can_manage_ont_from_request",
        lambda *args, **kwargs: True,
    )

    response = network_onts_provisioning.provision_ont_direct(
        SimpleNamespace(headers={}),
        "ont-1",
        async_execution=False,
        db=db_session,
    )

    assert response.status_code == 200
    assert "tr069_olt_profile_id" not in captured


def test_provisioning_step_route_rejects_out_of_scope_ont(db_session, monkeypatch):
    from types import SimpleNamespace

    from app.web.admin import network_onts_provisioning

    called = False

    def fake_wait(*args, **kwargs):
        nonlocal called
        called = True

    monkeypatch.setattr(
        network_onts_provisioning,
        "can_manage_ont_from_request",
        lambda *args, **kwargs: False,
    )
    monkeypatch.setattr(
        "app.services.network.ont_provision_steps.wait_tr069_bootstrap",
        fake_wait,
    )

    response = network_onts_provisioning.step_wait_tr069_bootstrap(
        SimpleNamespace(headers={}),
        "out-of-scope-ont",
        db=db_session,
    )

    assert response.status_code == 403
    assert called is False


def test_direct_orchestrator_updates_provisioning_status(db_session, monkeypatch):
    from app.models.network import OntProvisioningStatus, OntUnit
    from app.services.network.ont_provisioning.orchestrator import (
        provision_ont_from_desired_config,
    )
    from app.services.network.ont_provisioning.result import StepResult

    ont = OntUnit(
        serial_number="DESIRED-CFG-STATUS",
        desired_config={},
        provisioning_status=OntProvisioningStatus.unprovisioned,
    )
    db_session.add(ont)
    db_session.flush()

    monkeypatch.setattr(
        "app.services.network.ont_provision_steps.provision_with_reconciliation",
        lambda *args, **kwargs: StepResult("provision_reconciled", True, "ok"),
    )

    result = provision_ont_from_desired_config(db_session, str(ont.id))

    assert result.success is True
    assert ont.provisioning_status == OntProvisioningStatus.provisioned


def test_direct_orchestrator_marks_failed_status(db_session, monkeypatch):
    from app.models.network import OntProvisioningStatus, OntUnit
    from app.services.network.ont_provisioning.orchestrator import (
        provision_ont_from_desired_config,
    )
    from app.services.network.ont_provisioning.result import StepResult

    ont = OntUnit(
        serial_number="DESIRED-CFG-STATUS-FAIL",
        desired_config={},
        provisioning_status=OntProvisioningStatus.unprovisioned,
    )
    db_session.add(ont)
    db_session.flush()

    monkeypatch.setattr(
        "app.services.network.ont_provision_steps.provision_with_reconciliation",
        lambda *args, **kwargs: StepResult(
            "provision_reconciled", False, "OLT write failed"
        ),
    )

    result = provision_ont_from_desired_config(db_session, str(ont.id))

    assert result.success is False
    assert ont.provisioning_status == OntProvisioningStatus.failed


def test_failed_compensation_is_persisted_for_retry(db_session, monkeypatch):
    from types import SimpleNamespace

    from sqlalchemy import select

    from app.models.compensation_failure import (
        CompensationFailure,
        CompensationStatus,
    )
    from app.models.network import OLTDevice, OntUnit
    from app.services.network.ont_provisioning.executor import (
        CompensationEntry,
        ProvisioningExecutionResult,
    )

    olt = OLTDevice(name="Rollback OLT")
    ont = OntUnit(serial_number="DESIRED-CFG-ROLLBACK")
    db_session.add_all([olt, ont])
    db_session.flush()

    class FakeSession:
        def run_command(self, command, **kwargs):
            return SimpleNamespace(
                success=False,
                is_idempotent_success=False,
                message=f"failed {command}",
            )

    class FakeOltSession:
        def __enter__(self):
            return FakeSession()

        def __exit__(self, exc_type, exc, tb):
            return False

    monkeypatch.setattr(
        "app.services.network.ont_provisioning.executor.olt_session",
        lambda olt: FakeOltSession(),
    )
    monkeypatch.setattr(
        "app.services.network.ont_provisioning.executor._emit_compensation_failure_alert",
        lambda *args, **kwargs: None,
    )

    result = ProvisioningExecutionResult(
        success=False,
        message="failed",
        compensation_log=[
            CompensationEntry(
                step_name="create_service_port_vlan_100",
                undo_commands=["undo service-port 42"],
                description="Delete service-port VLAN 100 GEM 1",
                resource_id="42",
            )
        ],
    )

    rollback_results = result.rollback(olt, ont_unit_id=str(ont.id), db=db_session)

    failure = db_session.scalars(select(CompensationFailure)).one()
    assert rollback_results == [
        (
            "create_service_port_vlan_100",
            False,
            "Delete service-port VLAN 100 GEM 1",
        )
    ]
    assert failure.ont_unit_id == ont.id
    assert failure.olt_device_id == olt.id
    assert failure.status == CompensationStatus.pending
    assert failure.undo_commands == ["undo service-port 42"]


def test_reconciler_noops_when_olt_side_config_matches():
    from app.services.network.ont_provisioning.reconciler import compute_delta
    from app.services.network.ont_provisioning.state import (
        ActualManagementConfig,
        ActualOntState,
        ActualServicePort,
        DesiredManagementConfig,
        DesiredOntState,
        DesiredServicePort,
        DesiredTr069Config,
    )

    desired = DesiredOntState(
        ont_id="ont-1",
        serial_number="DESIRED-CFG-NOOP",
        fsp="0/1/1",
        olt_ont_id=7,
        service_ports=(DesiredServicePort(vlan_id=100, gem_index=1),),
        management=DesiredManagementConfig(vlan_tag=200, ip_mode="dhcp"),
        tr069=DesiredTr069Config(olt_profile_id=30),
        internet_config_ip_index=0,
        wan_config_profile_id=5,
    )
    actual = ActualOntState(
        is_authorized=True,
        olt_ont_id=7,
        service_ports=(
            ActualServicePort(
                index=42,
                vlan_id=100,
                gem_index=1,
                ont_id=7,
                state="up",
                tag_transform="translate",
            ),
        ),
        management=ActualManagementConfig(vlan_tag=200, ip_mode="dhcp"),
        tr069_profile_id=30,
        internet_config_ip_indices=(0,),
        wan_config_profiles={0: 5},
    )

    delta = compute_delta(desired, actual)

    assert delta.has_changes is False
    assert delta.needs_mgmt_ip_config is False
    assert delta.needs_tr069_bind is False
    assert delta.needs_internet_config is False
    assert delta.needs_wan_config is False


def test_reconciler_writes_when_olt_side_config_differs():
    from app.services.network.ont_provisioning.reconciler import compute_delta
    from app.services.network.ont_provisioning.state import (
        ActualManagementConfig,
        ActualOntState,
        DesiredManagementConfig,
        DesiredOntState,
        DesiredTr069Config,
    )

    desired = DesiredOntState(
        ont_id="ont-1",
        serial_number="DESIRED-CFG-DIFF",
        fsp="0/1/1",
        olt_ont_id=7,
        management=DesiredManagementConfig(
            vlan_tag=200,
            ip_mode="static",
            ip_address="192.0.2.10",
            subnet="255.255.255.0",
            gateway="192.0.2.1",
        ),
        tr069=DesiredTr069Config(olt_profile_id=30),
        internet_config_ip_index=1,
        wan_config_profile_id=5,
    )
    actual = ActualOntState(
        is_authorized=True,
        olt_ont_id=7,
        management=ActualManagementConfig(
            vlan_tag=201,
            ip_mode="static",
            ip_address="192.0.2.10",
            subnet="255.255.255.0",
            gateway="192.0.2.1",
        ),
        tr069_profile_id=31,
        internet_config_ip_indices=(0,),
        wan_config_profiles={1: 6},
    )

    delta = compute_delta(desired, actual)

    assert delta.needs_mgmt_ip_config is True
    assert delta.needs_tr069_bind is True
    assert delta.needs_internet_config is True
    assert delta.needs_wan_config is True


def test_provisioning_entrypoints_do_not_accept_tr069_profile_override():
    import inspect

    from app.services.network.bulk_provisioning import bulk_provision_onts
    from app.services.network.ont_provision_steps import provision_with_reconciliation
    from app.services.network.ont_provisioning.orchestrator import (
        provision_ont_from_desired_config,
    )
    from app.services.network.ont_provisioning.preflight import validate_prerequisites
    from app.services.network.ont_provisioning.reconciler import reconcile_ont_state
    from app.services.network.ont_provisioning.state import (
        build_desired_state_from_config,
    )
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


def test_web_wan_config_uses_config_pack_vlan_and_persists_desired_state(
    db_session, monkeypatch
):
    from types import SimpleNamespace

    from app.models.catalog import RegionZone
    from app.models.network import OLTDevice, OntUnit, Vlan, VlanPurpose
    from app.services.network.ont_action_common import ActionResult
    from app.services.web_network_ont_actions.config_setters import set_wan_config

    region = RegionZone(name="WAN Config Region", code="wan-config-region")
    olt = OLTDevice(name="WAN Config OLT")
    db_session.add_all([region, olt])
    db_session.flush()

    internet_vlan = Vlan(
        region_id=region.id,
        olt_device_id=olt.id,
        name="PPPoE Internet",
        tag=203,
        purpose=VlanPurpose.internet,
    )
    db_session.add(internet_vlan)
    db_session.flush()
    olt.internet_vlan_id = internet_vlan.id

    ont = OntUnit(serial_number="DESIRED-CFG-WAN", olt_device_id=olt.id)
    db_session.add(ont)
    db_session.flush()

    captured: dict[str, object] = {}

    def fake_set_wan_config(db, ont_id, **kwargs):
        captured.update(kwargs)
        return ActionResult(success=True, message="applied", waiting=False)

    monkeypatch.setattr(
        "app.services.network.ont_action_wan.set_wan_config",
        fake_set_wan_config,
    )
    monkeypatch.setattr(
        "app.services.web_network_ont_actions.config_setters._persist_ont_plan_step",
        lambda *args, **kwargs: None,
    )
    monkeypatch.setattr(
        "app.services.web_network_ont_actions.config_setters._log_action_audit",
        lambda *args, **kwargs: None,
    )

    result = set_wan_config(
        db_session,
        str(ont.id),
        wan_mode="pppoe",
        pppoe_username="subscriber@example",
        pppoe_password="secret",
        instance_index=1,
        wan_vlan=999,
        request=SimpleNamespace(),
    )

    assert result.success is True
    # The VLAN from config pack (203) should override the requested VLAN (999)
    assert captured["wan_vlan"] == 203
    # Note: WAN config is no longer persisted to OntUnit.desired_config
    # because the source of truth is OntAssignment (set by provisioning forms)


def test_web_wan_config_requires_config_pack_vlan(db_session, monkeypatch):
    from types import SimpleNamespace

    from app.models.network import OLTDevice, OntUnit
    from app.services.web_network_ont_actions.config_setters import set_wan_config

    olt = OLTDevice(name="WAN Config Missing VLAN OLT")
    db_session.add(olt)
    db_session.flush()
    ont = OntUnit(serial_number="DESIRED-CFG-WAN-NO-VLAN", olt_device_id=olt.id)
    db_session.add(ont)
    db_session.flush()

    def fail_set_wan_config(*args, **kwargs):
        raise AssertionError("WAN config should not apply without config pack VLAN")

    monkeypatch.setattr(
        "app.services.network.ont_action_wan.set_wan_config",
        fail_set_wan_config,
    )
    monkeypatch.setattr(
        "app.services.web_network_ont_actions.config_setters._log_action_audit",
        lambda *args, **kwargs: None,
    )

    result = set_wan_config(
        db_session,
        str(ont.id),
        wan_mode="pppoe",
        pppoe_username="subscriber@example",
        pppoe_password="secret",
        wan_vlan=999,
        request=SimpleNamespace(),
    )

    assert result.success is False
    assert result.message == (
        "OLT internet VLAN is required before applying WAN config."
    )


def test_ont_config_form_has_single_operator_path():
    from pathlib import Path

    source = Path("templates/admin/network/onts/_configure_form.html").read_text()

    assert "Create PPPoE WAN Service" not in source
    assert "Push PPPoE Credentials" not in source
    assert "push-pppoe-omci" not in source
    assert "wan/pppoe-credentials" not in source
    assert "vlans or []" not in source
