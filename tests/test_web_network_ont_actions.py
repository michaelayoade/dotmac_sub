from __future__ import annotations

import os
from ipaddress import IPv4Network
from types import SimpleNamespace

import pytest
from sqlalchemy import select

# SQLite doesn't support PostgreSQL partial unique indexes, so tests that
# create multiple OntAssignments per ONT will fail on SQLite.
# Tests use TEST_DATABASE_URL; if not set, conftest falls back to SQLite.
_uses_sqlite = "sqlite" in os.environ.get("TEST_DATABASE_URL", "sqlite")

from app.models.event_store import EventStore
from app.models.network import (
    ConfigMethod,
    CPEDevice,
    DeviceStatus,
    IpPool,
    IpProtocol,
    IPVersion,
    MgmtIpMode,
    OLTDevice,
    OntAssignment,
    OntBundleAssignment,
    OntBundleAssignmentStatus,
    OntConfigOverride,
    OntProfileWanService,
    OntProvisioningProfile,
    OntProvisioningStatus,
    OntUnit,
    PonPort,
    Vlan,
    WanConnectionType,
    WanMode,
)
from app.models.tr069 import Tr069AcsServer, Tr069CpeDevice
from app.schemas.network import OntAssignmentCreate
from app.services import network as network_service
from app.services import web_network_onts as web_network_onts_service
from app.services.network.ont_action_common import get_ont_client_or_error
from app.services.network.ont_action_device import get_running_config
from app.services.network.ont_inventory import return_ont_to_inventory
from app.services.web_network_ont_actions import (
    configure_form_context,
    operational_health_context,
    return_to_inventory,
    return_to_inventory_for_web,
    update_ont_config,
    wan_config_context,
    wifi_config_context,
)
from tests.legacy_ont_profile_link import seed_legacy_profile_link


def test_return_to_inventory_releases_ont_on_olt_and_keeps_inventory_active(
    db_session, subscriber, monkeypatch
):
    olt = OLTDevice(name="OLT-Return", mgmt_ip="198.51.100.50", is_active=True)
    db_session.add(olt)
    db_session.commit()

    pon = PonPort(olt_id=olt.id, name="0/2/1", is_active=True)
    db_session.add(pon)
    db_session.commit()

    ont = OntUnit(
        serial_number="RETURN-ONT-001",
        is_active=True,
        olt_device_id=olt.id,
        board="0/2",
        port="1",
        external_id="7",
        provisioning_status=OntProvisioningStatus.provisioned,
        wan_mode=WanMode.pppoe,
        config_method=ConfigMethod.tr069,
        ip_protocol=IpProtocol.dual_stack,
        pppoe_username="user1",
        pppoe_password="pass1",
        wan_remote_access=True,
        mgmt_ip_mode=MgmtIpMode.dhcp,
        mgmt_ip_address="192.0.2.10",
        mgmt_remote_access=True,
        voip_enabled=True,
    )
    db_session.add(ont)
    db_session.commit()

    assignment = network_service.ont_assignments.create(
        db_session,
        OntAssignmentCreate(
            ont_unit_id=ont.id,
            pon_port_id=pon.id,
            account_id=subscriber.id,
            active=True,
        ),
    )

    deleted_indexes: list[int] = []

    monkeypatch.setattr(
        "app.services.network.olt_ssh_service_ports.get_service_ports_for_ont",
        lambda *_args, **_kwargs: (
            True,
            "Found 2 service-port(s)",
            [SimpleNamespace(index=101), SimpleNamespace(index=202)],
        ),
    )
    monkeypatch.setattr(
        "app.services.network.olt_ssh_service_ports.delete_service_port",
        lambda _olt, index: (deleted_indexes.append(index) or True, "deleted"),
    )
    monkeypatch.setattr(
        "app.services.network.olt_ssh_ont.deauthorize_ont",
        lambda _olt, _fsp, _ont_id: (True, "ONT deleted"),
    )
    sync_calls: list[str] = []
    monkeypatch.setattr(
        "app.services.web_network_ont_autofind.sync_olt_autofind_candidates",
        lambda _db, olt_id: (
            sync_calls.append(olt_id) or True,
            "Found 1 unregistered ONT",
            {"discovered": 1},
        ),
    )

    result = return_to_inventory(db_session, str(ont.id))

    assert result.success is True
    assert "removed from OLT" in result.message
    assert deleted_indexes == [101, 202]
    assert sync_calls == [str(olt.id)]
    assert result.data is not None
    assert result.data["unconfigured_url"].startswith(
        "/admin/network/onts?view=unconfigured"
    )

    db_session.refresh(ont)
    db_session.refresh(assignment)

    assert ont.is_active is True
    assert ont.olt_device_id is None
    assert ont.board is None
    assert ont.port is None
    assert ont.external_id is None
    assert ont.provisioning_status == OntProvisioningStatus.unprovisioned
    assert ont.wan_mode is None
    assert ont.config_method is None
    assert ont.ip_protocol is None
    assert ont.pppoe_username is None
    assert ont.pppoe_password is None
    assert ont.wan_remote_access is False
    assert ont.mgmt_ip_mode is None
    assert ont.mgmt_ip_address is None
    assert ont.mgmt_remote_access is False
    assert ont.voip_enabled is False
    assert assignment.active is False
    cpe = db_session.scalars(
        select(CPEDevice).where(CPEDevice.serial_number == ont.serial_number).limit(1)
    ).first()
    assert cpe is not None
    assert cpe.status == DeviceStatus.active
    assert cpe.subscriber_id != subscriber.id
    assert cpe.service_address_id is None


def test_return_to_inventory_clears_bundle_assignment_and_overrides(
    db_session, subscriber, monkeypatch
):
    olt = OLTDevice(name="OLT-Return-Bundle", mgmt_ip="198.51.100.51", is_active=True)
    db_session.add(olt)
    db_session.commit()

    pon = PonPort(olt_id=olt.id, name="0/2/9", is_active=True)
    bundle = OntProvisioningProfile(name="Return Bundle", olt_device_id=olt.id, is_active=True)
    db_session.add_all([pon, bundle])
    db_session.commit()

    ont = OntUnit(
        serial_number="RETURN-BUNDLE-001",
        is_active=True,
        olt_device_id=olt.id,
        board="0/2",
        port="9",
        external_id="19",
        provisioning_status=OntProvisioningStatus.provisioned,
    )
    db_session.add(ont)
    db_session.commit()

    assignment = network_service.ont_assignments.create(
        db_session,
        OntAssignmentCreate(
            ont_unit_id=ont.id,
            pon_port_id=pon.id,
            account_id=subscriber.id,
            active=True,
        ),
    )
    db_session.add(
        OntBundleAssignment(
            ont_unit_id=ont.id,
            bundle_id=bundle.id,
            status=OntBundleAssignmentStatus.applied,
            is_active=True,
        )
    )
    db_session.add(
        OntConfigOverride(
            ont_unit_id=ont.id,
            field_name="wan.pppoe_username",
            value_json={"value": "override-user"},
        )
    )
    db_session.commit()

    monkeypatch.setattr(
        "app.services.network.olt_ssh_service_ports.get_service_ports_for_ont",
        lambda *_args, **_kwargs: (True, "none", []),
    )
    monkeypatch.setattr(
        "app.services.network.olt_ssh_ont.deauthorize_ont",
        lambda _olt, _fsp, _ont_id: (True, "ONT deleted"),
    )
    monkeypatch.setattr(
        "app.services.web_network_ont_autofind.sync_olt_autofind_candidates",
        lambda _db, olt_id: (True, "ok", {"discovered": 0}),
    )

    result = return_ont_to_inventory(db_session, str(ont.id))

    assert result.success is True
    db_session.refresh(ont)
    db_session.refresh(assignment)
    active_bundle = db_session.scalars(
        select(OntBundleAssignment)
        .where(OntBundleAssignment.ont_unit_id == ont.id)
        .where(OntBundleAssignment.is_active.is_(True))
    ).first()
    overrides = db_session.scalars(
        select(OntConfigOverride).where(OntConfigOverride.ont_unit_id == ont.id)
    ).all()
    assert assignment.active is False
    assert ont.provisioning_profile_id is None
    assert active_bundle is None
    assert overrides == []


def test_tr069_resolution_waits_for_first_inform(db_session, monkeypatch):
    ont = OntUnit(serial_number="WAIT-ACS-001", is_active=True)
    db_session.add(ont)
    db_session.commit()
    db_session.refresh(ont)

    monkeypatch.setattr(
        "app.services.network.ont_action_common.resolve_genieacs_with_reason",
        lambda *_args: (
            None,
            "No TR-069 device found in GenieACS for ONT serial 'WAIT-ACS-001'.",
        ),
    )

    resolved, error = get_ont_client_or_error(db_session, str(ont.id))

    assert resolved is None
    assert error is not None
    assert error.waiting is True
    assert error.data == {"waiting_reason": "next_inform", "serial": "WAIT-ACS-001"}
    assert "waiting for its first GenieACS inform" in error.message


def test_configure_form_context_scopes_vlans_and_mgmt_ips_to_ont_olt(
    db_session, region
):
    olt = OLTDevice(name="OLT-Configure", mgmt_ip="198.51.100.60", is_active=True)
    other_olt = OLTDevice(name="OLT-Other", mgmt_ip="198.51.100.61", is_active=True)
    db_session.add_all([olt, other_olt])
    db_session.commit()

    olt_vlan = Vlan(
        tag=450,
        name="Management OLT Configure",
        region_id=region.id,
        olt_device_id=olt.id,
        is_active=True,
    )
    global_vlan = Vlan(
        tag=451,
        name="Global Management",
        region_id=region.id,
        olt_device_id=None,
        is_active=True,
    )
    other_vlan = Vlan(
        tag=452,
        name="Management OLT Other",
        region_id=region.id,
        olt_device_id=other_olt.id,
        is_active=True,
    )
    pool = IpPool(
        name="Management Pool",
        ip_version=IPVersion.ipv4,
        cidr="10.45.0.0/30",
        gateway="10.45.0.1",
        is_active=True,
        olt_device_id=olt.id,
        vlan=olt_vlan,
    )
    global_pool = IpPool(
        name="Global Management Pool",
        ip_version=IPVersion.ipv4,
        cidr="10.46.0.0/30",
        gateway="10.46.0.1",
        is_active=True,
        olt_device_id=None,
        vlan=global_vlan,
    )
    other_pool = IpPool(
        name="Other OLT Management Pool",
        ip_version=IPVersion.ipv4,
        cidr="10.47.0.0/30",
        gateway="10.47.0.1",
        is_active=True,
        olt_device_id=other_olt.id,
        vlan=other_vlan,
    )
    profile = OntProvisioningProfile(
        name="Profile With Mgmt Pool",
        olt_device_id=olt.id,
        mgmt_ip_mode=MgmtIpMode.static_ip,
        mgmt_vlan_tag=450,
        mgmt_ip_pool=pool,
        is_active=True,
    )
    ont = OntUnit(
        serial_number="CONFIG-ONT-001",
        is_active=True,
        olt_device_id=olt.id,
        provisioning_profile=profile,
        mgmt_ip_mode=MgmtIpMode.static_ip,
    )
    db_session.add_all(
        [olt_vlan, global_vlan, other_vlan, pool, global_pool, other_pool, profile, ont]
    )
    db_session.commit()
    db_session.refresh(ont)

    context = configure_form_context(db_session, str(ont.id))

    assert [vlan.tag for vlan in context["vlans"]] == [450]
    assert context["mgmt_ip_pool"].id == pool.id
    assert [ip["address"] for ip in context["available_mgmt_ips"]] == ["10.45.0.2"]
    assert "10.46.0.2" not in [ip["address"] for ip in context["available_mgmt_ips"]]
    assert "10.47.0.2" not in [ip["address"] for ip in context["available_mgmt_ips"]]


def test_configure_form_context_uses_pon_assignment_olt_when_ont_fk_missing(
    db_session, region, subscriber
):
    olt = OLTDevice(name="OLT-Assignment", mgmt_ip="198.51.100.62", is_active=True)
    other_olt = OLTDevice(
        name="OLT-Assignment-Other", mgmt_ip="198.51.100.63", is_active=True
    )
    db_session.add_all([olt, other_olt])
    db_session.commit()

    pon_port = PonPort(olt_id=olt.id, name="0/1/1", is_active=True)
    db_session.add(pon_port)
    db_session.commit()

    olt_vlan = Vlan(
        tag=550,
        name="Assignment OLT Management",
        region_id=region.id,
        olt_device_id=olt.id,
        is_active=True,
    )
    other_vlan = Vlan(
        tag=551,
        name="Other Assignment OLT Management",
        region_id=region.id,
        olt_device_id=other_olt.id,
        is_active=True,
    )
    pool = IpPool(
        name="Assignment Management Pool",
        ip_version=IPVersion.ipv4,
        cidr="10.55.0.0/30",
        gateway="10.55.0.1",
        is_active=True,
        olt_device_id=None,
        vlan=olt_vlan,
    )
    other_pool = IpPool(
        name="Other Assignment Management Pool",
        ip_version=IPVersion.ipv4,
        cidr="10.56.0.0/30",
        gateway="10.56.0.1",
        is_active=True,
        olt_device_id=other_olt.id,
        vlan=other_vlan,
    )
    ont = OntUnit(
        serial_number="CONFIG-ONT-ASSIGN-001",
        is_active=True,
        olt_device_id=None,
        mgmt_ip_mode=MgmtIpMode.static_ip,
    )
    db_session.add_all([olt_vlan, other_vlan, pool, other_pool, ont])
    db_session.commit()

    # Assignment must be active for OLT resolution to use it (inactive
    # assignments are treated as stale and ignored for UI scoping).
    assignment = OntAssignment(
        ont_unit_id=ont.id,
        pon_port_id=pon_port.id,
        subscriber_id=subscriber.id,
        active=True,
    )
    db_session.add(assignment)
    db_session.commit()
    db_session.refresh(ont)

    context = configure_form_context(db_session, str(ont.id))

    assert [vlan.tag for vlan in context["vlans"]] == [550]
    assert [ip["address"] for ip in context["available_mgmt_ips"]] == ["10.55.0.2"]
    assert "10.56.0.2" not in [ip["address"] for ip in context["available_mgmt_ips"]]


def test_ont_contexts_use_effective_bundle_resolution_with_overrides(
    db_session, region
):
    olt = OLTDevice(name="OLT-Resolver", mgmt_ip="198.51.100.90", is_active=True)
    db_session.add(olt)
    db_session.commit()

    legacy_profile = OntProvisioningProfile(
        name="Legacy Profile",
        olt_device_id=olt.id,
        mgmt_ip_mode=MgmtIpMode.dhcp,
        mgmt_vlan_tag=700,
        wifi_enabled=False,
        wifi_ssid_template="legacy-ssid",
        is_active=True,
    )
    assigned_bundle = OntProvisioningProfile(
        name="Assigned Bundle",
        olt_device_id=olt.id,
        mgmt_ip_mode=MgmtIpMode.static_ip,
        mgmt_vlan_tag=701,
        wifi_enabled=True,
        wifi_ssid_template="bundle-ssid",
        wifi_security_mode="WPA2-Personal",
        wifi_channel="11",
        is_active=True,
    )
    db_session.add_all([legacy_profile, assigned_bundle])
    db_session.commit()

    db_session.add(
        OntProfileWanService(
            profile_id=assigned_bundle.id,
            name="Internet",
            s_vlan=777,
            connection_type=WanConnectionType.pppoe,
            is_active=True,
        )
    )
    ont = OntUnit(
        serial_number="RESOLVER-ONT-001",
        is_active=True,
        olt_device_id=olt.id,
        wan_mode=WanMode.static_ip,
        pppoe_username="legacy-user",
        mgmt_ip_mode=MgmtIpMode.dhcp,
        wifi_ssid="legacy-ont-ssid",
    )
    seed_legacy_profile_link(ont, legacy_profile)
    db_session.add(ont)
    db_session.commit()

    db_session.add(
        OntBundleAssignment(
            ont_unit_id=ont.id,
            bundle_id=assigned_bundle.id,
            status=OntBundleAssignmentStatus.applied,
            is_active=True,
        )
    )
    db_session.add(
        OntConfigOverride(
            ont_unit_id=ont.id,
            field_name="wan.pppoe_username",
            value_json={"value": "override-user"},
        )
    )
    db_session.commit()

    wan_context = wan_config_context(db_session, str(ont.id))
    wifi_context = wifi_config_context(db_session, str(ont.id))

    assert wan_context["desired_wan_config"]["wan_mode"] == "pppoe"
    assert wan_context["desired_wan_config"]["wan_vlan"] == "777"
    assert wan_context["desired_wan_config"]["pppoe_username"] == "override-user"
    assert wan_context["config_resolution"]["bundle"].id == assigned_bundle.id
    assert "pppoe_username" in wan_context["config_resolution"]["overrides"]
    assert wan_context["config_resolution"]["using_legacy_fallback"] is False

    assert wifi_context["desired_wifi_config"]["ssid"] == "bundle-ssid"
    assert wifi_context["desired_wifi_config"]["channel"] == "11"
    assert wifi_context["desired_wifi_config"]["security_mode"] == "WPA2-Personal"
    assert wifi_context["config_resolution"]["bundle"].id == assigned_bundle.id


def test_configure_form_context_uses_effective_bundle_values(db_session, region):
    olt = OLTDevice(name="OLT-Configure-Effective", mgmt_ip="198.51.100.92", is_active=True)
    db_session.add(olt)
    db_session.flush()

    bundle = OntProvisioningProfile(
        name="Configure Effective Bundle",
        olt_device_id=olt.id,
        mgmt_ip_mode=MgmtIpMode.static_ip,
        mgmt_vlan_tag=901,
        wifi_enabled=True,
        wifi_ssid_template="bundle-ssid",
        config_method=ConfigMethod.tr069,
        ip_protocol=IpProtocol.ipv4,
        is_active=True,
    )
    db_session.add(bundle)
    db_session.flush()

    db_session.add(
        OntProfileWanService(
            profile_id=bundle.id,
            name="Internet",
            s_vlan=902,
            connection_type=WanConnectionType.pppoe,
            is_active=True,
        )
    )
    ont = OntUnit(
        serial_number="CONFIG-EFFECTIVE-ONT-001",
        is_active=True,
        olt_device_id=olt.id,
        wan_mode=WanMode.static_ip,
        pppoe_username="legacy-user",
        mgmt_ip_mode=MgmtIpMode.dhcp,
    )
    db_session.add(ont)
    db_session.flush()

    db_session.add(
        OntBundleAssignment(
            ont_unit_id=ont.id,
            bundle_id=bundle.id,
            status=OntBundleAssignmentStatus.applied,
            is_active=True,
        )
    )
    db_session.add_all(
        [
            OntConfigOverride(
                ont_unit_id=ont.id,
                field_name="wan.pppoe_username",
                value_json={"value": "override-user"},
            ),
            OntConfigOverride(
                ont_unit_id=ont.id,
                field_name="management.ip_address",
                value_json={"value": "10.90.0.2"},
            ),
        ]
    )
    db_session.commit()

    context = configure_form_context(db_session, str(ont.id))

    assert context["wan_mode"] == "pppoe"
    assert context["wan_vlan_id"] == "902"
    assert context["pppoe_username"] == "override-user"
    assert context["mgmt_ip_mode"] == "static_ip"
    assert context["mgmt_vlan_id"] == "901"
    assert context["mgmt_ip_address"] == "10.90.0.2"
    assert context["config_method"] == "tr069"
    assert context["ip_protocol"] == "ipv4"


def test_configure_form_context_does_not_select_legacy_profile_fk_without_assignment(
    db_session, region
):
    olt = OLTDevice(name="OLT-Configure-No-Legacy-Fallback", mgmt_ip="198.51.100.93")
    db_session.add(olt)
    db_session.flush()

    legacy_profile = OntProvisioningProfile(
        name="Legacy Only Profile",
        olt_device_id=olt.id,
        is_active=True,
    )
    db_session.add(legacy_profile)
    db_session.flush()

    ont = OntUnit(
        serial_number="CONFIG-NO-LEGACY-FALLBACK-001",
        is_active=True,
        olt_device_id=olt.id,
    )
    seed_legacy_profile_link(ont, legacy_profile)
    db_session.add(ont)
    db_session.commit()

    context = configure_form_context(db_session, str(ont.id))

    assert context["selected_bundle_id"] == ""
    assert context["profile_preview"] is None


def test_update_ont_config_assigns_bundle_and_persists_core_overrides(
    db_session, region
):
    olt = OLTDevice(name="OLT-Author", mgmt_ip="198.51.100.91", is_active=True)
    db_session.add(olt)
    db_session.flush()
    wan_vlan_bundle = Vlan(
        tag=801,
        name="Bundle WAN",
        region_id=region.id,
        olt_device_id=olt.id,
        is_active=True,
    )
    wan_vlan_override = Vlan(
        tag=802,
        name="Override WAN",
        region_id=region.id,
        olt_device_id=olt.id,
        is_active=True,
    )
    mgmt_vlan_bundle = Vlan(
        tag=803,
        name="Bundle MGMT",
        region_id=region.id,
        olt_device_id=olt.id,
        is_active=True,
    )
    mgmt_vlan_override = Vlan(
        tag=804,
        name="Override MGMT",
        region_id=region.id,
        olt_device_id=olt.id,
        is_active=True,
    )
    bundle = OntProvisioningProfile(
        name="Author Bundle",
        olt_device_id=olt.id,
        mgmt_ip_mode=MgmtIpMode.dhcp,
        mgmt_vlan_tag=803,
        wifi_enabled=True,
        wifi_ssid_template="bundle-ssid",
        wifi_channel="6",
        wifi_security_mode="WPA2-Personal",
        config_method=ConfigMethod.tr069,
        ip_protocol=IpProtocol.ipv4,
        is_active=True,
    )
    db_session.add_all(
        [
            wan_vlan_bundle,
            wan_vlan_override,
            mgmt_vlan_bundle,
            mgmt_vlan_override,
            bundle,
        ]
    )
    db_session.commit()
    db_session.add(
        OntProfileWanService(
            profile_id=bundle.id,
            name="Internet",
            s_vlan=801,
            connection_type=WanConnectionType.pppoe,
            pppoe_username_template="bundle-user",
            is_active=True,
        )
    )
    ont = OntUnit(serial_number="AUTHOR-ONT-001", is_active=True, olt_device_id=olt.id)
    db_session.add(ont)
    db_session.commit()

    result = update_ont_config(
        db_session,
        str(ont.id),
        bundle_id=str(bundle.id),
        wan_mode="pppoe",
        wan_vlan_id=str(wan_vlan_override.id),
        config_method="tr069",
        ip_protocol="ipv4",
        pppoe_username="custom-user",
        mgmt_ip_mode="static_ip",
        mgmt_vlan_id=str(mgmt_vlan_override.id),
        mgmt_ip_address="10.80.0.2",
        wifi_enabled=True,
        wifi_ssid="bundle-ssid",
        wifi_channel="11",
        wifi_security_mode="WPA3-Personal",
        push_to_device=False,
    )

    assert result.success is True
    db_session.refresh(ont)
    assert ont.provisioning_profile_id is None

    assignment = db_session.scalars(
        select(OntBundleAssignment)
        .where(OntBundleAssignment.ont_unit_id == ont.id)
        .where(OntBundleAssignment.is_active.is_(True))
    ).first()
    assert assignment is not None
    assert assignment.bundle_id == bundle.id

    overrides = {
        row.field_name: row.value_json["value"]
        for row in db_session.scalars(
            select(OntConfigOverride).where(OntConfigOverride.ont_unit_id == ont.id)
        ).all()
    }
    assert overrides["wan.vlan_tag"] == "802"
    assert overrides["wan.pppoe_username"] == "custom-user"
    assert overrides["management.ip_mode"] == "static_ip"
    assert overrides["management.vlan_tag"] == "804"
    assert overrides["management.ip_address"] == "10.80.0.2"
    assert overrides["wifi.channel"] == "11"
    assert overrides["wifi.security_mode"] == "WPA3-Personal"
    assert "wifi.ssid" not in overrides
    assert "wan.wan_mode" not in overrides


def test_update_ont_config_manual_mode_clears_assignment_and_overrides(
    db_session, region
):
    olt = OLTDevice(name="OLT-Manual", mgmt_ip="198.51.100.92", is_active=True)
    db_session.add(olt)
    db_session.flush()
    bundle = OntProvisioningProfile(name="Manual Bundle", olt_device_id=olt.id, is_active=True)
    db_session.add(bundle)
    db_session.commit()

    ont = OntUnit(
        serial_number="AUTHOR-ONT-002",
        is_active=True,
        olt_device_id=olt.id,
    )
    db_session.add(ont)
    db_session.commit()
    db_session.add(
        OntBundleAssignment(
            ont_unit_id=ont.id,
            bundle_id=bundle.id,
            status=OntBundleAssignmentStatus.applied,
            is_active=True,
        )
    )
    db_session.add(
        OntConfigOverride(
            ont_unit_id=ont.id,
            field_name="wan.pppoe_username",
            value_json={"value": "custom-user"},
        )
    )
    db_session.commit()

    result = update_ont_config(
        db_session,
        str(ont.id),
        bundle_id="",
        wifi_enabled=True,
        push_to_device=False,
    )

    assert result.success is True
    db_session.refresh(ont)
    assert ont.provisioning_profile_id is None
    active_assignment = db_session.scalars(
        select(OntBundleAssignment)
        .where(OntBundleAssignment.ont_unit_id == ont.id)
        .where(OntBundleAssignment.is_active.is_(True))
    ).first()
    assert active_assignment is None
    remaining_overrides = db_session.scalars(
        select(OntConfigOverride).where(OntConfigOverride.ont_unit_id == ont.id)
    ).all()
    assert remaining_overrides == []


def test_update_ont_config_treats_active_assignment_as_bundle_managed_without_legacy_fk(
    db_session, region
):
    olt = OLTDevice(
        name="OLT-Bundle-Managed-No-Legacy-FK",
        mgmt_ip="198.51.100.94",
        is_active=True,
    )
    db_session.add(olt)
    db_session.flush()

    wan_vlan = Vlan(
        tag=806,
        name="Bundle WAN 806",
        region_id=region.id,
        olt_device_id=olt.id,
        is_active=True,
    )
    bundle = OntProvisioningProfile(
        name="Bundle Managed Without Legacy FK",
        olt_device_id=olt.id,
        config_method=ConfigMethod.tr069,
        is_active=True,
    )
    db_session.add_all([wan_vlan, bundle])
    db_session.flush()
    db_session.add(
        OntProfileWanService(
            profile_id=bundle.id,
            name="Internet",
            s_vlan=806,
            connection_type=WanConnectionType.pppoe,
            is_active=True,
        )
    )
    ont = OntUnit(
        serial_number="AUTHOR-ONT-NO-LEGACY-FK-001",
        is_active=True,
        olt_device_id=olt.id,
        provisioning_profile_id=None,
    )
    db_session.add(ont)
    db_session.flush()
    db_session.add(
        OntBundleAssignment(
            ont_unit_id=ont.id,
            bundle_id=bundle.id,
            status=OntBundleAssignmentStatus.applied,
            is_active=True,
        )
    )
    db_session.commit()

    result = update_ont_config(
        db_session,
        str(ont.id),
        bundle_id=None,
        wan_mode="pppoe",
        wan_vlan_id=str(wan_vlan.id),
        config_method="tr069",
        pppoe_username="override-user",
        push_to_device=False,
    )

    assert result.success is True
    db_session.refresh(ont)
    assert ont.pppoe_username is None
    assert ont.wan_mode is None
    overrides = {
        row.field_name: row.value_json["value"]
        for row in db_session.scalars(
            select(OntConfigOverride).where(OntConfigOverride.ont_unit_id == ont.id)
        ).all()
    }
    assert overrides["wan.pppoe_username"] == "override-user"
    assert "wan.vlan_tag" not in overrides


def test_update_ont_config_push_to_device_rejects_legacy_wan_push(db_session, region):
    olt = OLTDevice(name="OLT-Push-Effective", mgmt_ip="198.51.100.93", is_active=True)
    db_session.add(olt)
    db_session.flush()

    wan_vlan = Vlan(
        tag=805,
        name="Push WAN",
        region_id=region.id,
        olt_device_id=olt.id,
        is_active=True,
    )
    bundle = OntProvisioningProfile(
        name="Push Bundle",
        olt_device_id=olt.id,
        config_method=ConfigMethod.tr069,
        is_active=True,
    )
    db_session.add_all([wan_vlan, bundle])
    db_session.commit()

    db_session.add(
        OntProfileWanService(
            profile_id=bundle.id,
            name="Internet",
            s_vlan=805,
            connection_type=WanConnectionType.pppoe,
            pppoe_username_template="bundle-user",
            is_active=True,
        )
    )
    ont = OntUnit(serial_number="PUSH-ONT-001", is_active=True, olt_device_id=olt.id)
    db_session.add(ont)
    db_session.commit()

    result = update_ont_config(
        db_session,
        str(ont.id),
        bundle_id=str(bundle.id),
        wan_mode="pppoe",
        wan_vlan_id=str(wan_vlan.id),
        config_method="tr069",
        pppoe_username="override-user",
        pppoe_password="inline-secret",
        push_to_device=True,
    )

    assert result.success is False
    assert "direct WAN/PPPoE pushes are disabled" in result.message
    db_session.refresh(ont)
    assert ont.pppoe_username is None


def test_management_ip_choices_prefers_expected_olt_management_network_from_name_alias(
    db_session,
):
    olt = OLTDevice(name="BOI Asokoro OLT 1", is_active=True, mgmt_ip=None)
    db_session.add(olt)
    db_session.commit()

    managed_pool = IpPool(
        name="BOI Management Range",
        ip_version=IPVersion.ipv4,
        cidr="172.20.100.8/30",
        gateway="172.20.100.9",
        is_active=True,
        olt_device_id=olt.id,
        vlan_id=None,
    )
    other_pool = IpPool(
        name="Other OLT Range",
        ip_version=IPVersion.ipv4,
        cidr="10.55.0.0/30",
        gateway="10.55.0.1",
        is_active=True,
        olt_device_id=olt.id,
        vlan_id=None,
    )
    ont = OntUnit(
        serial_number="CONFIG-ONT-ALIAS-001",
        is_active=True,
        olt_device_id=olt.id,
        mgmt_ip_mode=MgmtIpMode.static_ip,
    )
    db_session.add_all([managed_pool, other_pool, ont])
    db_session.commit()

    choices = web_network_onts_service.management_ip_choices_for_ont(
        db_session, ont, limit=25
    )

    addresses = [entry["address"] for entry in choices["available_mgmt_ips"]]
    assert addresses == ["172.20.100.10"]
    assert choices["mgmt_ip_pool"] is not None
    assert choices["mgmt_ip_pool"].id == managed_pool.id


def test_management_ip_choices_use_effective_override_selected_ip(
    db_session, monkeypatch
):
    olt = OLTDevice(name="Override OLT", is_active=True, mgmt_ip=None)
    db_session.add(olt)
    db_session.flush()

    pool = IpPool(
        name="Override Pool",
        ip_version=IPVersion.ipv4,
        cidr="10.60.0.0/30",
        gateway="10.60.0.1",
        is_active=True,
        olt_device_id=olt.id,
        vlan_id=None,
    )
    ont = OntUnit(
        serial_number="CONFIG-ONT-OVERRIDE-IP-001",
        is_active=True,
        olt_device_id=olt.id,
        mgmt_ip_address=None,
    )
    db_session.add_all([pool, ont])
    db_session.flush()
    db_session.add(
        OntConfigOverride(
            ont_unit_id=ont.id,
            field_name="management.ip_address",
            value_json={"value": "10.60.0.2"},
        )
    )
    db_session.commit()

    captured: dict[str, object] = {}

    def fake_choices(db, pool_id, ont_id, selected_ip=None, limit=20):
        captured["selected_ip"] = selected_ip
        return {"choices": [{"address": "10.60.0.2"}]}

    monkeypatch.setattr(
        "app.services.web_network_onts_provisioning.available_static_ipv4_choices",
        fake_choices,
    )

    choices = web_network_onts_service.management_ip_choices_for_ont(
        db_session, ont, limit=25
    )

    assert captured["selected_ip"] == "10.60.0.2"
    assert [entry["address"] for entry in choices["available_mgmt_ips"]] == [
        "10.60.0.2"
    ]


@pytest.mark.parametrize(
    "name,managed_network,managed_address,wrong_address,serial_suffix",
    [
        (
            "Garki Huawei OLT 172.16.201.2/24",
            "172.16.201.0/24",
            "172.16.201.2",
            "10.201.0.2",
            "GARKI",
        ),
        (
            "BOI Huawei OLT 172.20.100.9/30",
            "172.20.100.8/30",
            "172.20.100.10",
            "10.220.0.2",
            "BOI",
        ),
        ("Gudu Huawei OLT", "172.16.205.0/24", "172.16.205.2", "10.205.0.2", "GUDU"),
        ("Karsana Huawei OLT", "172.16.203.0/24", "172.16.203.2", "10.203.0.2", "KARS"),
        ("Jabi Huawei OLT", "172.16.204.0/24", "172.16.204.2", "10.204.0.2", "JABI"),
        (
            "Gwarimpa Huawei OLT",
            "172.16.207.0/24",
            "172.16.207.2",
            "10.207.0.2",
            "GWMR",
        ),
        ("SPDC Huawei OLT", "172.16.210.0/24", "172.16.210.2", "10.210.0.2", "SPDC"),
    ],
)
def test_management_ip_choices_uses_actual_olt_name_network_mapping(
    db_session,
    name,
    managed_network,
    managed_address,
    wrong_address,
    serial_suffix,
):
    olt = OLTDevice(name=name, is_active=True, mgmt_ip=None)
    db_session.add(olt)
    db_session.commit()

    managed_network_obj = IPv4Network(managed_network, strict=False)
    managed_gateway = str(managed_network_obj.network_address + 1)
    managed_pool = IpPool(
        name="Managed OLT Range",
        ip_version=IPVersion.ipv4,
        cidr=managed_network,
        gateway=managed_gateway,
        is_active=True,
        olt_device_id=olt.id,
        vlan_id=None,
    )
    other_pool = IpPool(
        name="Other OLT Range",
        ip_version=IPVersion.ipv4,
        cidr="10.200.0.0/30",
        gateway=wrong_address,
        is_active=True,
        olt_device_id=olt.id,
        vlan_id=None,
    )
    ont = OntUnit(
        serial_number=f"CONFIG-ONT-{serial_suffix}",
        is_active=True,
        olt_device_id=olt.id,
        mgmt_ip_mode=MgmtIpMode.static_ip,
    )
    db_session.add_all([managed_pool, other_pool, ont])
    db_session.commit()

    choices = web_network_onts_service.management_ip_choices_for_ont(
        db_session, ont, limit=25
    )

    addresses = [entry["address"] for entry in choices["available_mgmt_ips"]]
    assert managed_address in addresses
    assert wrong_address not in addresses
    assert choices["mgmt_ip_pool"] is not None
    assert choices["mgmt_ip_pool"].id == managed_pool.id


def test_management_ip_choices_prefers_name_alias_when_mgmt_ip_is_unmatched(
    db_session,
):
    olt = OLTDevice(
        name="Garki Huawei OLT 172.16.201.2/24",
        mgmt_ip="172.16.153.23",
        is_active=True,
    )
    db_session.add(olt)
    db_session.commit()

    managed_pool = IpPool(
        name="Garki Managed Range",
        ip_version=IPVersion.ipv4,
        cidr="172.16.201.0/24",
        gateway="172.16.201.1",
        is_active=True,
        olt_device_id=olt.id,
        vlan_id=None,
    )
    fallback_pool = IpPool(
        name="Unmatched Range",
        ip_version=IPVersion.ipv4,
        cidr="172.16.153.0/24",
        gateway="172.16.153.1",
        is_active=True,
        olt_device_id=olt.id,
        vlan_id=None,
    )
    ont = OntUnit(
        serial_number="CONFIG-ONT-UNMATCHED",
        is_active=True,
        olt_device_id=olt.id,
        mgmt_ip_mode=MgmtIpMode.static_ip,
    )
    db_session.add_all([managed_pool, fallback_pool, ont])
    db_session.commit()

    choices = web_network_onts_service.management_ip_choices_for_ont(
        db_session, ont, limit=25
    )

    addresses = [entry["address"] for entry in choices["available_mgmt_ips"]]
    assert "172.16.201.2" in addresses
    assert "172.16.153.2" not in addresses
    assert choices["mgmt_ip_pool"] is not None
    assert choices["mgmt_ip_pool"].id == managed_pool.id


def test_management_ip_choices_prefers_direct_olt_link_over_inactive_assignments(
    db_session,
):
    olt = OLTDevice(
        name="Garki Huawei OLT",
        mgmt_ip="172.16.201.2",
        is_active=True,
    )
    legacy_olt = OLTDevice(
        name="Legacy OLT",
        mgmt_ip="172.16.153.2",
        is_active=True,
    )
    db_session.add_all([olt, legacy_olt])
    db_session.commit()

    pon_port = PonPort(olt_id=legacy_olt.id, name="legacy/0/1", is_active=True)
    db_session.add(pon_port)
    db_session.commit()

    bad_pool = IpPool(
        name="Legacy Range",
        ip_version=IPVersion.ipv4,
        cidr="172.16.153.0/24",
        gateway="172.16.153.1",
        is_active=True,
        olt_device_id=legacy_olt.id,
        vlan_id=None,
    )
    expected_pool = IpPool(
        name="Garki Managed Range",
        ip_version=IPVersion.ipv4,
        cidr="172.16.201.0/24",
        gateway="172.16.201.1",
        is_active=True,
        olt_device_id=olt.id,
        vlan_id=None,
    )

    ont = OntUnit(
        serial_number="CONFIG-ONT-STALE-ASSIGN",
        is_active=True,
        olt_device_id=olt.id,
        mgmt_ip_mode=MgmtIpMode.static_ip,
    )
    db_session.add_all([ont, bad_pool, expected_pool])
    db_session.commit()

    assignment = OntAssignment(
        ont_unit_id=ont.id,
        pon_port_id=pon_port.id,
        active=False,
    )
    db_session.add(assignment)
    db_session.commit()

    choices = web_network_onts_service.management_ip_choices_for_ont(
        db_session, ont, limit=25
    )
    addresses = [entry["address"] for entry in choices["available_mgmt_ips"]]
    assert any(addr.startswith("172.16.201.") for addr in addresses)
    assert not any(addr.startswith("172.16.153.") for addr in addresses)
    assert choices["mgmt_ip_pool"] is not None
    assert choices["mgmt_ip_pool"].id == expected_pool.id


@pytest.mark.skipif(
    _uses_sqlite,
    reason="SQLite does not support PostgreSQL partial unique indexes (allows only one assignment per ONT)",
)
def test_management_ip_choices_prefers_active_assignment_over_inactive(
    db_session,
):
    garki_olt = OLTDevice(
        name="Garki Huawei OLT", mgmt_ip="172.16.201.2", is_active=True
    )
    legacy_olt = OLTDevice(name="Legacy OLT", mgmt_ip="172.16.153.2", is_active=True)
    db_session.add_all([garki_olt, legacy_olt])
    db_session.commit()

    pon_port = PonPort(olt_id=legacy_olt.id, name="legacy/0/1", is_active=True)
    db_session.add(pon_port)
    db_session.commit()

    legacy_pool = IpPool(
        name="Legacy Range",
        ip_version=IPVersion.ipv4,
        cidr="172.16.153.0/24",
        gateway="172.16.153.1",
        is_active=True,
        olt_device_id=legacy_olt.id,
        vlan_id=None,
    )
    expected_pool = IpPool(
        name="Garki Managed Range",
        ip_version=IPVersion.ipv4,
        cidr="172.16.201.0/24",
        gateway="172.16.201.1",
        is_active=True,
        olt_device_id=garki_olt.id,
        vlan_id=None,
    )

    ont = OntUnit(
        serial_number="CONFIG-ONT-ACTIVE-ASSIGN",
        is_active=True,
        olt_device_id=garki_olt.id,
        mgmt_ip_mode=MgmtIpMode.static_ip,
    )
    db_session.add_all([ont, legacy_pool, expected_pool])
    db_session.commit()

    stale_assignment = OntAssignment(
        ont_unit_id=ont.id,
        pon_port_id=pon_port.id,
        active=False,
    )
    active_assignment = OntAssignment(
        ont_unit_id=ont.id,
        pon_port_id=pon_port.id,
        active=True,
    )
    db_session.add_all([stale_assignment, active_assignment])
    db_session.commit()

    choices = web_network_onts_service.management_ip_choices_for_ont(
        db_session, ont, limit=25
    )
    addresses = [entry["address"] for entry in choices["available_mgmt_ips"]]
    assert any(addr.startswith("172.16.201.") for addr in addresses)
    assert not any(addr.startswith("172.16.153.") for addr in addresses)
    assert choices["mgmt_ip_pool"] is not None
    assert choices["mgmt_ip_pool"].id == expected_pool.id


def test_management_ip_choices_ignores_stale_inactive_assignment_without_explicit_olt(
    db_session,
):
    legacy_olt = OLTDevice(name="Legacy OLT", mgmt_ip="172.16.153.2", is_active=True)
    db_session.add(legacy_olt)
    db_session.commit()

    pon_port = PonPort(olt_id=legacy_olt.id, name="legacy/0/1", is_active=True)
    db_session.add(pon_port)
    db_session.commit()

    legacy_pool = IpPool(
        name="Legacy Range",
        ip_version=IPVersion.ipv4,
        cidr="172.16.153.0/24",
        gateway="172.16.153.1",
        is_active=True,
        olt_device_id=legacy_olt.id,
        vlan_id=None,
    )
    db_session.add(legacy_pool)
    db_session.commit()

    ont = OntUnit(
        serial_number="CONFIG-ONT-STALE-NO-OLT",
        is_active=True,
        mgmt_ip_mode=MgmtIpMode.static_ip,
    )
    db_session.add(ont)
    db_session.commit()

    assignment = OntAssignment(
        ont_unit_id=ont.id,
        pon_port_id=pon_port.id,
        active=False,
    )
    db_session.add(assignment)
    db_session.commit()

    choices = web_network_onts_service.management_ip_choices_for_ont(
        db_session, ont, limit=25
    )

    assert choices["mgmt_ip_pool"] is None
    assert choices["available_mgmt_ips"] == []
    assert choices["mgmt_ip_choice_message"] == "No active IPv4 pools are available."


def test_management_ip_choices_ignores_profile_pool_outside_expected_olt_range(
    db_session,
):
    olt = OLTDevice(
        name="Jabi Huawei OLT 172.16.204.1/24",
        mgmt_ip="172.16.153.23",
        is_active=True,
    )
    db_session.add(olt)
    db_session.commit()

    managed_pool = IpPool(
        name="Jabi Managed Range",
        ip_version=IPVersion.ipv4,
        cidr="172.16.204.0/24",
        gateway="172.16.204.1",
        is_active=True,
        olt_device_id=olt.id,
        vlan_id=None,
    )
    wrong_pool = IpPool(
        name="Legacy Wrong Range",
        ip_version=IPVersion.ipv4,
        cidr="172.16.153.0/24",
        gateway="172.16.153.1",
        is_active=True,
        olt_device_id=olt.id,
        vlan_id=None,
    )
    profile = OntProvisioningProfile(
        name="Profile With Wrong Range",
        olt_device_id=olt.id,
        mgmt_ip_mode=MgmtIpMode.static_ip,
        mgmt_ip_pool=wrong_pool,
        is_active=True,
    )
    ont = OntUnit(
        serial_number="CONFIG-ONT-WRONG-PROFILE",
        is_active=True,
        olt_device_id=olt.id,
        provisioning_profile=profile,
        mgmt_ip_mode=MgmtIpMode.static_ip,
    )
    db_session.add_all([managed_pool, wrong_pool, profile, ont])
    db_session.commit()

    choices = web_network_onts_service.management_ip_choices_for_ont(
        db_session, ont, limit=25
    )

    addresses = [entry["address"] for entry in choices["available_mgmt_ips"]]
    assert any(addr.startswith("172.16.204.") for addr in addresses)
    assert not any(addr.startswith("172.16.153.") for addr in addresses)
    assert choices["mgmt_ip_pool"] is not None
    assert choices["mgmt_ip_pool"].id == managed_pool.id


def test_running_config_reads_internet_gateway_device_paths(db_session, monkeypatch):
    ont = OntUnit(serial_number="IGD-CONFIG-001", is_active=True)
    db_session.add(ont)
    db_session.commit()
    db_session.refresh(ont)

    device_doc = {
        "InternetGatewayDevice": {
            "DeviceInfo": {
                "Manufacturer": {"_value": "Huawei"},
                "ModelName": {"_value": "HG8245H"},
                "SerialNumber": {"_value": "IGD-CONFIG-001"},
            },
            "WANDevice": {
                "1": {
                    "WANConnectionDevice": {
                        "1": {
                            "WANPPPConnection": {
                                "1": {
                                    "ExternalIPAddress": {"_value": "100.64.1.10"},
                                    "Username": {"_value": "cust@example"},
                                    "ConnectionStatus": {"_value": "Connected"},
                                }
                            }
                        }
                    }
                }
            },
            "LANDevice": {
                "1": {
                    "WLANConfiguration": {
                        "1": {
                            "SSID": {"_value": "DotMac"},
                            "TotalAssociations": {"_value": 3},
                        }
                    }
                }
            },
        }
    }

    class FakeClient:
        def get_device(self, _device_id):
            return device_doc

        def extract_parameter_value(self, device, parameter_path):
            current = device
            for part in parameter_path.split("."):
                if not isinstance(current, dict):
                    return None
                current = current.get(part)
                if current is None:
                    return None
            if isinstance(current, dict):
                return current.get("_value")
            return current

    monkeypatch.setattr(
        "app.services.network.ont_action_common.resolve_genieacs_with_reason",
        lambda *_args: ((FakeClient(), "igd-device-id"), "resolved"),
    )

    result = get_running_config(db_session, str(ont.id))

    assert result.success is True
    assert result.data["device_info"]["Manufacturer"] == "Huawei"
    assert result.data["wan"]["WAN IP"] == "100.64.1.10"
    assert result.data["wan"]["Username"] == "cust@example"
    assert result.data["wifi"]["SSID"] == "DotMac"
    assert result.data["wifi"]["Connected Clients"] == 3


def test_operational_health_context_surfaces_olt_acs_and_pppoe_state(
    db_session, monkeypatch
):
    olt = OLTDevice(name="OLT-Health", mgmt_ip="198.51.100.55", is_active=True)
    db_session.add(olt)
    db_session.commit()

    ont = OntUnit(
        serial_number="HEALTH-ONT-001",
        is_active=True,
        olt_device_id=olt.id,
        board="0/1",
        port="2",
        external_id="11",
        pppoe_username="health@example",
    )
    db_session.add(ont)
    db_session.commit()

    acs = Tr069AcsServer(name="ACS", base_url="http://acs.example.test")
    db_session.add(acs)
    db_session.commit()

    db_session.add(
        Tr069CpeDevice(
            acs_server_id=acs.id,
            ont_unit_id=ont.id,
            serial_number=ont.serial_number,
            genieacs_device_id="HEALTH-ACS-ID",
            connection_request_url="http://198.51.100.10:7547/",
            is_active=True,
        )
    )
    db_session.commit()

    monkeypatch.setattr(
        "app.services.web_network_ont_actions._config_snapshot_service",
        lambda: SimpleNamespace(list_for_ont=lambda *_args, **_kwargs: []),
    )

    context = operational_health_context(db_session, str(ont.id))
    checks = {check["label"]: check for check in context["operational_checks"]}

    assert checks["OLT linked"]["ok"] is True
    assert checks["F/S/P known"]["message"] == "0/1/2"
    assert checks["OLT ONT-ID known"]["message"] == "11"
    assert checks["ACS linked"]["message"] == "HEALTH-ACS-ID"
    assert checks["Connection request URL"]["ok"] is True
    assert checks["PPPoE stored"]["message"] == "health@example"


def test_return_to_inventory_keeps_local_state_when_olt_delete_fails(
    db_session, monkeypatch
):
    olt = OLTDevice(name="OLT-Return-Fail", mgmt_ip="198.51.100.51", is_active=True)
    db_session.add(olt)
    db_session.commit()

    pon = PonPort(olt_id=olt.id, name="0/2/1", is_active=True)
    db_session.add(pon)
    db_session.commit()

    ont = OntUnit(
        serial_number="RETURN-ONT-FAIL-001",
        is_active=True,
        olt_device_id=olt.id,
        board="0/2",
        port="1",
        external_id="9",
        provisioning_status=OntProvisioningStatus.provisioned,
        pppoe_username="keepme",
    )
    db_session.add(ont)
    db_session.commit()

    assignment = OntAssignment(ont_unit_id=ont.id, pon_port_id=pon.id, active=True)
    db_session.add(assignment)
    db_session.commit()

    monkeypatch.setattr(
        "app.services.network.olt_ssh_service_ports.get_service_ports_for_ont",
        lambda *_args, **_kwargs: (True, "Found 0 service-port(s)", []),
    )
    monkeypatch.setattr(
        "app.services.network.olt_ssh_ont.deauthorize_ont",
        lambda _olt, _fsp, _ont_id: (False, "OLT rejected delete"),
    )

    result = return_to_inventory(db_session, str(ont.id))

    assert result.success is False
    assert "Failed to delete ONT from OLT" in result.message

    db_session.refresh(ont)
    db_session.refresh(assignment)

    assert ont.is_active is True
    assert ont.external_id == "9"
    assert ont.provisioning_status == OntProvisioningStatus.provisioned
    assert ont.pppoe_username == "keepme"
    assert assignment.active is True


def test_operational_health_uses_effective_pppoe_username(
    db_session, monkeypatch
):
    acs = Tr069AcsServer(
        name="ACS-Health-Effective",
        base_url="http://acs.example.test",
    )
    olt = OLTDevice(name="OLT-Health-Effective", mgmt_ip="198.51.100.91", is_active=True)
    db_session.add_all([acs, olt])
    db_session.commit()

    ont = OntUnit(
        serial_number="HEALTH-EFFECTIVE-001",
        is_active=True,
        olt_device_id=olt.id,
        board="0/1",
        port="2",
        external_id="11",
        pppoe_username=None,
    )
    db_session.add(ont)
    db_session.commit()

    db_session.add(
        Tr069CpeDevice(
            acs_server_id=acs.id,
            ont_unit_id=ont.id,
            serial_number=ont.serial_number,
            genieacs_device_id="HEALTH-ACS-EFFECTIVE",
            connection_request_url="http://198.51.100.10:7547/",
            is_active=True,
        )
    )
    db_session.commit()

    monkeypatch.setattr(
        "app.services.web_network_ont_actions._config_snapshot_service",
        lambda: SimpleNamespace(list_for_ont=lambda *_args, **_kwargs: []),
    )
    monkeypatch.setattr(
        "app.services.web_network_ont_actions.operational.resolve_effective_ont_config",
        lambda *_args, **_kwargs: {
            "values": {
                "wan_mode": "pppoe",
                "pppoe_username": "override-health@example",
            }
        },
    )

    context = operational_health_context(db_session, str(ont.id))
    checks = {check["label"]: check for check in context["operational_checks"]}

    assert checks["PPPoE stored"]["ok"] is True
    assert checks["PPPoE stored"]["message"] == "override-health@example"


def test_return_to_inventory_succeeds_with_ambiguous_cpe_serial_match(
    db_session, subscriber, monkeypatch
):
    olt = OLTDevice(
        name="OLT-Return-Ambiguous", mgmt_ip="198.51.100.60", is_active=True
    )
    db_session.add(olt)
    db_session.commit()

    pon = PonPort(olt_id=olt.id, name="0/2/1", is_active=True)
    db_session.add(pon)
    db_session.commit()

    ont = OntUnit(
        serial_number="RETURN-ONT-AMB-001",
        is_active=True,
        olt_device_id=olt.id,
        board="0/2",
        port="1",
        external_id="17",
        provisioning_status=OntProvisioningStatus.provisioned,
    )
    db_session.add(ont)
    db_session.commit()

    assignment = network_service.ont_assignments.create(
        db_session,
        OntAssignmentCreate(
            ont_unit_id=ont.id,
            pon_port_id=pon.id,
            subscriber_id=subscriber.id,
            active=True,
        ),
    )

    inventory_subscriber = network_service.cpe.get_inventory_subscriber(db_session)
    if inventory_subscriber is None:
        inventory_subscriber = network_service.cpe._get_or_create_inventory_subscriber(
            db_session
        )
        db_session.commit()

    duplicate_cpe = CPEDevice(
        subscriber_id=inventory_subscriber.id,
        serial_number=ont.serial_number,
        status=DeviceStatus.inactive,
    )
    db_session.add(duplicate_cpe)
    db_session.commit()

    monkeypatch.setattr(
        "app.services.network.olt_ssh_service_ports.get_service_ports_for_ont",
        lambda *_args, **_kwargs: (True, "Found 0 service-port(s)", []),
    )
    monkeypatch.setattr(
        "app.services.network.olt_ssh_ont.deauthorize_ont",
        lambda _olt, _fsp, _ont_id: (True, "ONT deleted"),
    )
    monkeypatch.setattr(
        "app.services.web_network_ont_autofind.sync_olt_autofind_candidates",
        lambda _db, _olt_id: (
            True,
            "Found 0 unregistered ONTs",
            {"discovered": 0},
        ),
    )

    result = return_to_inventory(db_session, str(ont.id))

    assert result.success is True
    db_session.refresh(ont)
    db_session.refresh(assignment)
    assert ont.is_active is True
    assert assignment.active is False
    alert = db_session.scalars(
        select(EventStore)
        .where(EventStore.event_type == "network.alert")
        .order_by(EventStore.created_at.desc())
        .limit(1)
    ).first()
    assert alert is not None
    assert alert.payload["code"] == "ambiguous_ont_cpe_serial"
    assert alert.payload["ont_id"] == str(ont.id)


def test_new_inventory_return_refreshes_autofind_and_returns_unconfigured_url(
    db_session, subscriber, monkeypatch
):
    olt = OLTDevice(name="OLT-New-Return", mgmt_ip="198.51.100.61", is_active=True)
    db_session.add(olt)
    db_session.commit()

    pon = PonPort(olt_id=olt.id, name="0/2/2", is_active=True)
    db_session.add(pon)
    db_session.commit()

    ont = OntUnit(
        serial_number="RETURN-ONT-NEW-001",
        is_active=True,
        olt_device_id=olt.id,
        board="0/2",
        port="2",
        external_id="18",
        provisioning_status=OntProvisioningStatus.provisioned,
        lan_gateway_ip="192.168.88.1",
        lan_subnet_mask="255.255.255.0",
        lan_dhcp_enabled=True,
        lan_dhcp_start="192.168.88.10",
        lan_dhcp_end="192.168.88.200",
        wifi_ssid="OldCustomerWifi",
        wifi_password="encrypted-old-password",
    )
    db_session.add(ont)
    db_session.commit()

    active = OntAssignment(
        ont_unit_id=ont.id,
        pon_port_id=pon.id,
        subscriber_id=subscriber.id,
        active=True,
    )
    db_session.add(active)
    db_session.commit()

    cpe = CPEDevice(
        subscriber_id=subscriber.id,
        serial_number=ont.serial_number,
        status=DeviceStatus.active,
    )
    db_session.add(cpe)
    db_session.commit()

    cleanup_calls: list[str] = []
    monkeypatch.setattr(
        "app.services.web_network_ont_actions._cleanup_olt_state_for_return",
        lambda _db, ont_id: (cleanup_calls.append(ont_id) or True, ["cleaned"], []),
    )
    sync_calls: list[str] = []
    monkeypatch.setattr(
        "app.services.web_network_ont_autofind.sync_olt_autofind_candidates",
        lambda _db, olt_id: (
            sync_calls.append(olt_id) or True,
            "Found 1 unregistered ONT",
            {"discovered": 1},
        ),
    )

    result = return_ont_to_inventory(db_session, str(ont.id))

    assert result.success is True
    assert cleanup_calls == [str(ont.id)]
    assert sync_calls == [str(olt.id)]
    assert result.data is not None
    assert result.data["unconfigured_url"].startswith(
        "/admin/network/onts?view=unconfigured"
    )
    db_session.refresh(active)
    db_session.refresh(ont)
    db_session.refresh(cpe)
    assert active.active is False
    assert ont.olt_device_id is None
    assert ont.lan_gateway_ip is None
    assert ont.lan_subnet_mask is None
    assert ont.lan_dhcp_enabled is None
    assert ont.lan_dhcp_start is None
    assert ont.lan_dhcp_end is None
    assert ont.wifi_ssid is None
    assert ont.wifi_password is None
    assert cpe.status == DeviceStatus.active
    assert cpe.subscriber_id != subscriber.id
    assert cpe.service_address_id is None


def test_return_to_inventory_for_web_clears_local_state_without_olt_context(
    db_session, subscriber
):
    ont = OntUnit(
        serial_number="RETURN-ONT-WEB-LOCAL-001",
        is_active=True,
        provisioning_status=OntProvisioningStatus.provisioned,
        pppoe_username="old-user",
        lan_gateway_ip="192.168.55.1",
        wifi_ssid="OldWifi",
        wifi_password="old-secret",
    )
    db_session.add(ont)
    db_session.commit()

    active = OntAssignment(
        ont_unit_id=ont.id,
        subscriber_id=subscriber.id,
        active=True,
    )
    db_session.add(active)
    db_session.commit()

    result = return_to_inventory_for_web(db_session, str(ont.id))

    assert result.success is True
    db_session.refresh(active)
    db_session.refresh(ont)
    assert active.active is False
    assert ont.provisioning_status == OntProvisioningStatus.unprovisioned
    assert ont.pppoe_username is None
    assert ont.lan_gateway_ip is None
    assert ont.wifi_ssid is None
    assert ont.wifi_password is None


def test_return_to_inventory_for_web_clears_bundle_assignment_and_overrides_without_olt_context(
    db_session, subscriber
):
    bundle = OntProvisioningProfile(name="Web Return Bundle", is_active=True)
    db_session.add(bundle)
    db_session.commit()

    ont = OntUnit(
        serial_number="RETURN-ONT-WEB-BUNDLE-001",
        is_active=True,
        provisioning_status=OntProvisioningStatus.provisioned,
        pppoe_username="old-user",
        wifi_ssid="OldWifi",
    )
    db_session.add(ont)
    db_session.commit()
    db_session.add(
        OntAssignment(
            ont_unit_id=ont.id,
            subscriber_id=subscriber.id,
            active=True,
        )
    )
    db_session.add(
        OntBundleAssignment(
            ont_unit_id=ont.id,
            bundle_id=bundle.id,
            status=OntBundleAssignmentStatus.applied,
            is_active=True,
        )
    )
    db_session.add(
        OntConfigOverride(
            ont_unit_id=ont.id,
            field_name="wifi.ssid",
            value_json={"value": "override-ssid"},
        )
    )
    db_session.commit()

    result = return_to_inventory_for_web(db_session, str(ont.id))

    assert result.success is True
    db_session.refresh(ont)
    active_bundle = db_session.scalars(
        select(OntBundleAssignment)
        .where(OntBundleAssignment.ont_unit_id == ont.id)
        .where(OntBundleAssignment.is_active.is_(True))
    ).first()
    inactive_bundle = db_session.scalars(
        select(OntBundleAssignment)
        .where(OntBundleAssignment.ont_unit_id == ont.id)
        .limit(1)
    ).first()
    overrides = db_session.scalars(
        select(OntConfigOverride).where(OntConfigOverride.ont_unit_id == ont.id)
    ).all()
    assert ont.provisioning_profile_id is None
    assert active_bundle is None
    assert inactive_bundle is not None
    assert inactive_bundle.status == OntBundleAssignmentStatus.superseded
    assert inactive_bundle.superseded_at is not None
    assert overrides == []
