"""Coverage for direct ONT desired-config provisioning behavior."""

from __future__ import annotations

import pytest


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
    from app.models.network import (
        OLTDevice,
        OltLineProfile,
        OltLineProfileGemMapping,
        OltOnuTypeProfileMapping,
        OltServiceProfile,
        OntAssignment,
        OntUnit,
        Vlan,
        VlanPurpose,
    )
    from app.models.tr069 import Tr069AcsServer
    from app.services.network.effective_ont_config import (
        internet_wcd_index_from_effective_values,
        resolve_effective_ont_config,
    )

    region = RegionZone(name="Test Region", code="desired-config")
    acs = Tr069AcsServer(
        name="Config Pack ACS",
        base_url="http://config-pack-acs.example",
        is_active=True,
    )
    olt = OLTDevice(name="OLT Defaults")
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
    # Set config_pack JSON (source of truth for OLT config pack)
    olt.config_pack = {
        "tr069_olt_profile_id": 30,
        "internet_config_ip_index": 0,
        "wan_config_profile_id": 5,
        "cr_username": "pack-cr-user",
        "cr_password": "pack-cr-pass",
        "internet_vlan_id": str(internet_vlan.id),
        "management_vlan_id": str(mgmt_vlan.id),
    }

    ont = OntUnit(
        serial_number="DESIRED-CFG-002",
        model="EG8145V5",
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
    db_session.add_all(
        [
            OltLineProfile(olt_id=olt.id, profile_id=10, name="LINE"),
            OltServiceProfile(olt_id=olt.id, profile_id=20, name="EG8145V5"),
        ]
    )
    db_session.flush()
    db_session.add_all(
        [
            OltLineProfileGemMapping(
                olt_id=olt.id,
                line_profile_id=10,
                source="service_port",
                source_key="service-port:vlan:100:gem:1",
                gem_index=1,
                vlan_id=100,
                usage_count=12,
            ),
            OltLineProfileGemMapping(
                olt_id=olt.id,
                line_profile_id=10,
                source="service_port",
                source_key="service-port:vlan:200:gem:2",
                gem_index=2,
                vlan_id=200,
                usage_count=12,
            ),
            OltOnuTypeProfileMapping(
                olt_id=olt.id,
                equipment_id="EG8145V5",
                line_profile_id=10,
                service_profile_id=20,
                wan_provisioning_mode="omci_wan_config",
                internet_config_ip_index=1,
                wan_config_profile_id=0,
                pppoe_wcd_index=2,
                mgmt_wcd_index=1,
                primary_wan_service="INTERNET",
            ),
        ]
    )
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
    assert values["mgmt_gem_index"] == 2
    assert values["authorization_line_profile_id"] == 10
    assert values["authorization_service_profile_id"] == 20
    assert values["tr069_acs_server_id"] == str(acs.id)
    assert values["tr069_olt_profile_id"] == 30
    assert values["cr_username"] == "pack-cr-user"
    assert values["cr_password"] == "pack-cr-pass"
    assert values["internet_config_ip_index"] == 1
    assert values["wan_config_profile_id"] == 0
    assert values["pppoe_wcd_index"] == 2
    assert values["mgmt_wcd_index"] == 1
    assert internet_wcd_index_from_effective_values(values) == 2
    assert values["primary_wan_service"] == "INTERNET"
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


def test_effective_config_backfills_management_network_from_ip_pool(db_session):
    from app.models.network import IpPool, IPv4Address, IPVersion, OntUnit
    from app.services.network.effective_ont_config import resolve_effective_ont_config

    pool = IpPool(
        name="Resolved Mgmt Pool",
        cidr="172.16.201.0/24",
        gateway="172.16.201.1",
        ip_version=IPVersion.ipv4,
        is_active=True,
    )
    ont = OntUnit(
        serial_number="DESIRED-CFG-MGMT-POOL",
        desired_config={
            "management": {
                "ip_mode": "static_ip",
                "ip_address": "172.16.201.145",
            }
        },
    )
    db_session.add_all([pool, ont])
    db_session.flush()
    db_session.add(
        IPv4Address(
            address="172.16.201.145",
            pool_id=pool.id,
            is_reserved=True,
            ont_unit_id=ont.id,
            allocation_type="management",
        )
    )
    db_session.flush()

    values = resolve_effective_ont_config(db_session, ont)["values"]

    assert values["mgmt_ip_address"] == "172.16.201.145"
    assert values["mgmt_subnet"] == "255.255.255.0"
    assert values["mgmt_gateway"] == "172.16.201.1"


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
    assert result.failed_step == "ont_lookup"
    assert calls == []


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
    monkeypatch.setattr(
        "app.services.network.ont_provisioning.preflight.validate_prerequisites",
        lambda *args, **kwargs: {"ready_to_provision": True, "checks": []},
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
    olt = OLTDevice(name="Saved Config OLT")
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
    # Set config_pack JSON (source of truth for OLT config pack)
    olt.config_pack = {
        "tr069_olt_profile_id": 30,
        "cr_username": "cr-user",
        "cr_password": encrypt_credential("cr-pass"),
        "internet_vlan_id": str(internet_vlan.id),
    }

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
        "app.services.network.ont_provision_steps.genieacs_service",
        SimpleNamespace(
            set_connection_request_credentials=lambda *args, **kwargs: SimpleNamespace(
                success=True,
                message="credentials pushed",
            ),
            set_lan_config=lambda *args, **kwargs: SimpleNamespace(
                success=True,
                message="lan pushed",
            ),
            set_wifi_config=lambda *args, **kwargs: SimpleNamespace(
                success=True,
                message="wifi pushed",
            ),
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
    olt = OLTDevice(name="ACS Only OLT")
    db_session.add_all([acs, olt])
    db_session.flush()
    olt.tr069_acs_server_id = acs.id
    # Set config_pack JSON (source of truth for OLT config pack)
    olt.config_pack = {"tr069_olt_profile_id": 30}

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


# NOTE: test_manual_step_bind_tr069_does_not_persist_profile_override was removed
# because step_bind_tr069 was removed as part of consolidating to reconciliation-only
# provisioning. TR-069 binding now happens via provision_with_reconciliation().


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
    monkeypatch.setattr(
        "app.services.network.ont_provisioning.preflight.validate_prerequisites",
        lambda *args, **kwargs: {"ready_to_provision": True, "checks": []},
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
    monkeypatch.setattr(
        "app.services.network.ont_provisioning.preflight.validate_prerequisites",
        lambda *args, **kwargs: {"ready_to_provision": True, "checks": []},
    )

    result = provision_ont_from_desired_config(db_session, str(ont.id))

    assert result.success is False
    assert ont.provisioning_status == OntProvisioningStatus.failed


def test_direct_orchestrator_blocks_before_olt_write_when_acs_not_ready(
    db_session, monkeypatch
):
    from app.models.network import OntProvisioningStatus, OntUnit
    from app.services.network.ont_provisioning.orchestrator import (
        provision_ont_from_desired_config,
    )

    ont = OntUnit(
        serial_number="DESIRED-CFG-ACS-BLOCK",
        desired_config={},
        provisioning_status=OntProvisioningStatus.unprovisioned,
    )
    db_session.add(ont)
    db_session.flush()

    monkeypatch.setattr(
        "app.services.network.ont_provisioning.preflight.validate_prerequisites",
        lambda *args, **kwargs: {
            "ready_to_provision": False,
            "checks": [
                {
                    "name": "ACS connection",
                    "status": "fail",
                    "message": "Authorize the ONT and wait for ACS inform before provisioning",
                }
            ],
        },
    )
    monkeypatch.setattr(
        "app.services.network.ont_provision_steps.provision_with_reconciliation",
        lambda *args, **kwargs: pytest.fail("OLT provisioning should not start"),
    )

    result = provision_ont_from_desired_config(db_session, str(ont.id))

    assert result.success is False
    assert result.failed_step == "preflight"
    assert result.steps[0].step_name == "preflight"
    assert ont.provisioning_status == OntProvisioningStatus.failed


def test_preflight_requires_acs_inform_before_provisioning(db_session, monkeypatch):
    from app.models.network import OntAuthorizationStatus, OntUnit
    from app.services.network.ont_provisioning import preflight

    ont = OntUnit(
        serial_number="DESIRED-CFG-ACS-PREFLIGHT",
        authorization_status=OntAuthorizationStatus.authorized,
    )
    db_session.add(ont)
    db_session.flush()
    monkeypatch.setattr(
        preflight,
        "resolve_effective_ont_config",
        lambda *args, **kwargs: {
            "values": {
                "tr069_acs_server_id": "00000000-0000-0000-0000-000000000001",
                "tr069_olt_profile_id": 7,
                "mgmt_vlan": 201,
                "authorization_line_profile_id": 10,
                "authorization_service_profile_id": 20,
            },
            "config_pack": None,
        },
    )

    result = preflight.validate_prerequisites(db_session, str(ont.id))
    acs_connection = next(
        check for check in result["checks"] if check["name"] == "ACS connection"
    )

    assert result["ready_to_provision"] is False
    assert acs_connection["status"] == "fail"
    assert "wait for ACS inform" in acs_connection["message"]


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
    # Set config_pack JSON (source of truth for OLT config pack)
    olt.config_pack = {"internet_vlan_id": str(internet_vlan.id)}

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


def test_web_wan_config_uses_requested_vlan_when_config_pack_missing(
    db_session, monkeypatch
):
    from types import SimpleNamespace

    from app.models.network import OLTDevice, OntUnit
    from app.services.network.ont_action_common import ActionResult
    from app.services.web_network_ont_actions.config_setters import set_wan_config

    olt = OLTDevice(name="WAN Config Missing VLAN OLT")
    db_session.add(olt)
    db_session.flush()
    ont = OntUnit(serial_number="DESIRED-CFG-WAN-NO-VLAN", olt_device_id=olt.id)
    db_session.add(ont)
    db_session.flush()

    captured = {}

    def fake_set_wan_config(*args, **kwargs):
        captured.update(kwargs)
        return ActionResult(success=True, message="WAN config applied")

    monkeypatch.setattr(
        "app.services.network.ont_action_wan.set_wan_config",
        fake_set_wan_config,
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

    assert result.success is True
    assert captured["wan_vlan"] == 999


def test_web_wan_config_routes_pppoe_to_omci_when_enabled(db_session, monkeypatch):
    from types import SimpleNamespace

    from app.models.catalog import RegionZone
    from app.models.network import OLTDevice, OntUnit, Vlan, VlanPurpose
    from app.services.web_network_ont_actions.config_setters import set_wan_config

    region = RegionZone(name="WAN OMCI Region", code="wan-omci-region")
    olt = OLTDevice(name="WAN OMCI OLT")
    db_session.add_all([region, olt])
    db_session.flush()

    internet_vlan = Vlan(
        region_id=region.id,
        olt_device_id=olt.id,
        name="Internet",
        tag=203,
        purpose=VlanPurpose.internet,
    )
    db_session.add(internet_vlan)
    db_session.flush()
    olt.config_pack = {
        "internet_vlan_id": str(internet_vlan.id),
        "pppoe_wcd_index": 2,
        "internet_config_ip_index": 0,
    }

    ont = OntUnit(serial_number="OMCI-WAN-001", olt_device_id=olt.id)
    db_session.add(ont)
    db_session.flush()

    calls: list[tuple[str, dict[str, object]]] = []

    class FakeAdapter:
        def configure_internet_config(self, fsp, ont_id, *, ip_index=0):
            calls.append(
                (
                    "internet",
                    {"fsp": fsp, "ont_id": ont_id, "ip_index": ip_index},
                )
            )
            return SimpleNamespace(success=True, message="internet ok")

        def configure_pppoe(
            self,
            fsp,
            ont_id,
            *,
            ip_index,
            vlan_id,
            username,
            password,
        ):
            calls.append(
                (
                    "pppoe",
                    {
                        "fsp": fsp,
                        "ont_id": ont_id,
                        "ip_index": ip_index,
                        "vlan_id": vlan_id,
                        "username": username,
                        "password": password,
                    },
                )
            )
            return SimpleNamespace(success=True, message="pppoe ok")

    monkeypatch.setattr(
        "app.services.web_network_ont_actions.config_setters.get_olt_write_mode_enabled",
        lambda db: True,
    )
    monkeypatch.setattr(
        "app.services.web_network_ont_actions.config_setters.get_pppoe_provisioning_method",
        lambda db: "auto",
    )
    monkeypatch.setattr(
        "app.services.network.ont_provisioning.context.resolve_olt_context",
        lambda db, ont_id: (
            SimpleNamespace(olt=olt, fsp="0/2/11", olt_ont_id=13),
            "",
        ),
    )
    monkeypatch.setattr(
        "app.services.network.olt_protocol_adapters.get_protocol_adapter",
        lambda olt: FakeAdapter(),
    )
    monkeypatch.setattr(
        "app.services.web_network_ont_actions.config_setters._log_action_audit",
        lambda *args, **kwargs: None,
    )
    monkeypatch.setattr(
        "app.services.web_network_ont_actions.config_setters._persist_ont_plan_step",
        lambda *args, **kwargs: None,
    )

    result = set_wan_config(
        db_session,
        str(ont.id),
        wan_mode="pppoe",
        pppoe_username="100025868",
        pppoe_password="secret",
        instance_index=1,
    )

    assert result.success is True
    assert result.waiting is True
    assert result.data["delivery_transport"] == "olt_omci"
    assert result.data["ip_index"] == 1
    assert calls == [
        ("internet", {"fsp": "0/2/11", "ont_id": 13, "ip_index": 1}),
        (
            "pppoe",
            {
                "fsp": "0/2/11",
                "ont_id": 13,
                "ip_index": 1,
                "vlan_id": 203,
                "username": "100025868",
                "password": "secret",
            },
        ),
    ]


def test_ont_config_form_has_single_operator_path():
    from pathlib import Path

    source = Path("templates/admin/network/onts/_configure_form.html").read_text()
    panel = Path(
        "templates/admin/network/onts/_apply_device_config_panel.html"
    ).read_text()

    assert "Create PPPoE WAN Service" not in source
    assert "Push PPPoE Credentials" not in source
    assert "push-pppoe-omci" not in source
    assert "wan/pppoe-credentials" not in source
    assert "vlans or []" not in source
    assert 'hx-indicator="find .config-spinner"' not in source
    assert 'hx-indicator="#config-spinner-wan"' in source
    assert 'name="push_to_device" value="true"' in source
    assert "Save and apply device changes" in source
    assert "/wan/probe" not in panel
    assert "/wan/ensure-instance" not in panel
    assert "/wan/normalize" not in panel
    assert "/wan-remote-access" in panel
    assert "/mgmt-remote-access" in panel
    assert "/web-credentials" in panel
    assert "/http-management" in panel
    assert "/connection-request-credentials" in panel


def test_onu_mode_remote_access_change_pushes_to_device(db_session, monkeypatch):
    from starlette.datastructures import FormData

    from app.models.network import OntUnit
    from app.services.network.ont_action_common import ActionResult
    from app.services.network.ont_features import OntFeatureService
    from app.services.network.ont_web_forms import update_onu_mode_from_form

    ont = OntUnit(serial_number="REMOTE-ACCESS-001")
    db_session.add(ont)
    db_session.flush()
    db_session.commit()

    monkeypatch.setattr(
        "app.services.network.ont_web_forms.network_service.ont_units.get_including_inactive",
        lambda db, entity_id: ont,
    )
    calls = []

    def _fake_toggle(db, ont_id, *, enabled):
        calls.append((ont_id, enabled))
        return ActionResult(success=True, message="WAN SSH access enabled.")

    monkeypatch.setattr(OntFeatureService, "toggle_wan_remote_access", _fake_toggle)

    result = update_onu_mode_from_form(
        db_session,
        str(ont.id),
        FormData(
            {
                "onu_mode": "routing",
                "wan_mode": "dhcp",
                "wan_remote_access": "true",
            }
        ),
    )

    assert result.error is None
    assert ont.desired_config["access"]["wan_remote"] is True
    assert calls == [(str(ont.id), True)]


def test_onu_mode_remote_access_push_failure_returns_error(db_session, monkeypatch):
    from starlette.datastructures import FormData

    from app.models.network import OntUnit
    from app.services.network.ont_action_common import ActionResult
    from app.services.network.ont_features import OntFeatureService
    from app.services.network.ont_web_forms import update_onu_mode_from_form

    ont = OntUnit(serial_number="REMOTE-ACCESS-002")
    db_session.add(ont)
    db_session.flush()
    db_session.commit()

    monkeypatch.setattr(
        "app.services.network.ont_web_forms.network_service.ont_units.get_including_inactive",
        lambda db, entity_id: ont,
    )
    monkeypatch.setattr(
        OntFeatureService,
        "toggle_wan_remote_access",
        lambda db, ont_id, *, enabled: ActionResult(
            success=False,
            message="ONT has no ACS server configured. Cannot push remote access config.",
        ),
    )

    result = update_onu_mode_from_form(
        db_session,
        str(ont.id),
        FormData(
            {
                "onu_mode": "routing",
                "wan_mode": "dhcp",
                "wan_remote_access": "true",
            }
        ),
    )

    assert result.error == (
        "ONT has no ACS server configured. Cannot push remote access config."
    )


def test_mgmt_remote_access_applies_iphost_without_global_remote_access(
    db_session, monkeypatch
):
    from app.models.network import MgmtIpMode, OntAssignment, OntUnit
    from app.services.web_network_ont_actions.config_setters import (
        set_mgmt_remote_access,
    )

    ont = OntUnit(
        serial_number="MGMT-REMOTE-001",
        desired_config={"management": {"ip_mode": "dhcp"}},
    )
    db_session.add(ont)
    db_session.flush()
    assignment = OntAssignment(
        ont_unit_id=ont.id,
        active=True,
        mgmt_ip_mode=MgmtIpMode.dhcp,
    )
    db_session.add(assignment)
    db_session.flush()

    iphost_calls = []

    def _fake_configure(db, ont_id, ip_mode="dhcp", **kwargs):
        iphost_calls.append((ont_id, ip_mode, kwargs))
        return True, "IPHOST configured"

    monkeypatch.setattr(
        "app.services.web_network_ont_actions.config_setters.configure_management_ip",
        _fake_configure,
    )

    result = set_mgmt_remote_access(db_session, str(ont.id), enabled=True)

    assert result.success is True
    assert iphost_calls == [
        (
            str(ont.id),
            "dhcp",
            {"ip_address": None, "subnet": None, "gateway": None},
        )
    ]
    assert ont.desired_config["access"]["mgmt_remote"] is True


def test_wan_config_uses_submitted_vlan_when_config_pack_missing(
    db_session, monkeypatch
):
    from app.models.network import OntUnit
    from app.services.network.ont_action_common import ActionResult
    from app.services.web_network_ont_actions.config_setters import set_wan_config

    ont = OntUnit(serial_number="WAN-VLAN-FALLBACK-001")
    db_session.add(ont)
    db_session.flush()

    calls = []

    def _fake_set_wan_config(db, ont_id, **kwargs):
        calls.append(kwargs)
        return ActionResult(success=True, message="WAN configured")

    monkeypatch.setattr(
        "app.services.network.ont_action_wan.set_wan_config",
        _fake_set_wan_config,
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
        wan_mode="dhcp",
        wan_vlan=321,
    )

    assert result.success is True
    assert calls[0]["wan_vlan"] == 321


def test_apply_saved_service_config_pushes_dhcp_enable_defensively_when_lan_unset(
    db_session, monkeypatch
):
    """DHCP server enable is pushed when desired_config explicitly enables it."""
    from types import SimpleNamespace

    from app.models.catalog import RegionZone
    from app.models.network import OLTDevice, OntAssignment, OntUnit, Vlan, VlanPurpose
    from app.models.tr069 import Tr069AcsServer
    from app.services.credential_crypto import encrypt_credential
    from app.services.network.ont_provision_steps import apply_saved_service_config

    region = RegionZone(name="LAN Default Region", code="lan-default-region")
    acs = Tr069AcsServer(
        name="LAN Default ACS",
        base_url="http://lan-default-acs.example",
        is_active=True,
    )
    olt = OLTDevice(name="LAN Default OLT")
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
    olt.config_pack = {
        "tr069_olt_profile_id": 30,
        "cr_username": "cr-user",
        "cr_password": encrypt_credential("cr-pass"),
        "internet_vlan_id": str(internet_vlan.id),
    }

    ont = OntUnit(
        serial_number="LAN-DEFAULT-DHCP",
        olt_device_id=olt.id,
        desired_config={
            "wan": {
                "mode": "pppoe",
                "pppoe_username": "cust",
                "pppoe_password": "secret",
            },
            "lan": {"dhcp_enabled": True},
        },
    )
    db_session.add(ont)
    db_session.flush()
    db_session.add(OntAssignment(ont_unit_id=ont.id, active=True))
    db_session.flush()

    lan_calls: list[dict] = []

    monkeypatch.setattr(
        "app.services.network.ont_provision_steps.get_pppoe_provisioning_method",
        lambda db: "tr069",
    )
    monkeypatch.setattr(
        "app.services.network.ont_action_network.probe_wan_capabilities",
        lambda db, ont_id: SimpleNamespace(success=True, message="probed"),
    )
    monkeypatch.setattr(
        "app.services.network.ont_action_wan.set_pppoe_credentials",
        lambda db, ont_id, **kwargs: SimpleNamespace(
            success=True, message="pppoe ok", waiting=False
        ),
    )

    def fake_set_lan_config(db, ont_id, **kwargs):
        lan_calls.append(kwargs)
        return SimpleNamespace(success=True, message="lan ok")

    monkeypatch.setattr(
        "app.services.network.ont_provision_steps.genieacs_service",
        SimpleNamespace(
            set_connection_request_credentials=lambda *a, **kw: SimpleNamespace(
                success=True, message="cr ok"
            ),
            set_lan_config=fake_set_lan_config,
            set_wifi_config=lambda *a, **kw: SimpleNamespace(
                success=True, message="wifi ok"
            ),
        ),
    )

    result = apply_saved_service_config(db_session, str(ont.id))

    assert result.success is True, result.message
    assert len(lan_calls) == 1
    assert lan_calls[0]["dhcp_enabled"] is True


def test_apply_saved_service_config_respects_explicit_dhcp_disable(
    db_session, monkeypatch
):
    """If LAN DHCP is explicitly False in desired_config, False is pushed."""
    from types import SimpleNamespace

    from app.models.catalog import RegionZone
    from app.models.network import OLTDevice, OntAssignment, OntUnit, Vlan, VlanPurpose
    from app.models.tr069 import Tr069AcsServer
    from app.services.credential_crypto import encrypt_credential
    from app.services.network.ont_provision_steps import apply_saved_service_config

    region = RegionZone(name="DHCP Off Region", code="dhcp-off-region")
    acs = Tr069AcsServer(
        name="DHCP Off ACS",
        base_url="http://dhcp-off-acs.example",
        is_active=True,
    )
    olt = OLTDevice(name="DHCP Off OLT")
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
    olt.config_pack = {
        "tr069_olt_profile_id": 30,
        "cr_username": "cr-user",
        "cr_password": encrypt_credential("cr-pass"),
        "internet_vlan_id": str(internet_vlan.id),
    }

    ont = OntUnit(
        serial_number="DHCP-OFF-EXPLICIT",
        olt_device_id=olt.id,
        desired_config={
            "wan": {
                "mode": "pppoe",
                "pppoe_username": "cust",
                "pppoe_password": "secret",
            },
            "lan": {"dhcp_enabled": False},
        },
    )
    db_session.add(ont)
    db_session.flush()
    db_session.add(OntAssignment(ont_unit_id=ont.id, active=True))
    db_session.flush()

    lan_calls: list[dict] = []

    monkeypatch.setattr(
        "app.services.network.ont_provision_steps.get_pppoe_provisioning_method",
        lambda db: "tr069",
    )
    monkeypatch.setattr(
        "app.services.network.ont_action_network.probe_wan_capabilities",
        lambda db, ont_id: SimpleNamespace(success=True, message="probed"),
    )
    monkeypatch.setattr(
        "app.services.network.ont_action_wan.set_pppoe_credentials",
        lambda db, ont_id, **kwargs: SimpleNamespace(
            success=True, message="pppoe ok", waiting=False
        ),
    )

    def fake_set_lan_config(db, ont_id, **kwargs):
        lan_calls.append(kwargs)
        return SimpleNamespace(success=True, message="lan ok")

    monkeypatch.setattr(
        "app.services.network.ont_provision_steps.genieacs_service",
        SimpleNamespace(
            set_connection_request_credentials=lambda *a, **kw: SimpleNamespace(
                success=True, message="cr ok"
            ),
            set_lan_config=fake_set_lan_config,
            set_wifi_config=lambda *a, **kw: SimpleNamespace(
                success=True, message="wifi ok"
            ),
        ),
    )

    result = apply_saved_service_config(db_session, str(ont.id))

    assert result.success is True, result.message
    assert len(lan_calls) == 1
    assert lan_calls[0]["dhcp_enabled"] is False
