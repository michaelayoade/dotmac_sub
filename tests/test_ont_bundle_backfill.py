from __future__ import annotations

from sqlalchemy import select

from app.models.network import (
    ConfigMethod,
    IpProtocol,
    MgmtIpMode,
    OLTDevice,
    OntBundleAssignment,
    OntBundleAssignmentStatus,
    OntConfigOverride,
    OntConfigOverrideSource,
    OntProfileWanService,
    OntProvisioningProfile,
    OntProvisioningStatus,
    OntUnit,
    Vlan,
    WanConnectionType,
    WanMode,
)
from app.services.network.ont_bundle_backfill import (
    apply_backfill_plan,
    build_backfill_plan,
    run_backfill,
)
from tests.legacy_ont_profile_link import seed_legacy_profile_link


def test_build_backfill_plan_marks_existing_assignment_as_already_migrated(db_session):
    olt = OLTDevice(name="OLT-Backfill-Existing", mgmt_ip="198.51.100.130", is_active=True)
    db_session.add(olt)
    db_session.flush()

    bundle = OntProvisioningProfile(
        name="Existing Bundle",
        olt_device_id=olt.id,
        is_active=True,
    )
    ont = OntUnit(
        serial_number="BF-EXIST-001",
        is_active=True,
        olt_device_id=olt.id,
    )
    db_session.add_all([bundle, ont])
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

    plan = build_backfill_plan(db_session, ont)

    assert plan.outcome == "already_migrated"
    assert plan.bundle_id == str(bundle.id)


def test_build_backfill_plan_collects_sparse_overrides_and_secret_warnings(
    db_session, region
):
    olt = OLTDevice(name="OLT-Backfill", mgmt_ip="198.51.100.131", is_active=True)
    db_session.add(olt)
    db_session.flush()

    wan_vlan = Vlan(
        tag=700,
        name="WAN",
        region_id=region.id,
        olt_device_id=olt.id,
        is_active=True,
    )
    mgmt_vlan = Vlan(
        tag=710,
        name="Mgmt",
        region_id=region.id,
        olt_device_id=olt.id,
        is_active=True,
    )
    bundle = OntProvisioningProfile(
        name="Residential Bundle",
        olt_device_id=olt.id,
        config_method=ConfigMethod.omci,
        ip_protocol=IpProtocol.ipv4,
        mgmt_ip_mode=MgmtIpMode.dhcp,
        mgmt_vlan_tag=710,
        wifi_enabled=True,
        wifi_ssid_template="bundle-ssid",
        wifi_channel="6",
        wifi_security_mode="WPA2-Personal",
        is_active=True,
    )
    db_session.add_all([wan_vlan, mgmt_vlan, bundle])
    db_session.flush()

    db_session.add(
        OntProfileWanService(
            profile_id=bundle.id,
            name="Internet",
            s_vlan=700,
            connection_type=WanConnectionType.pppoe,
            pppoe_username_template="bundle-user",
            is_active=True,
        )
    )

    ont = OntUnit(
        serial_number="BF-PLAN-001",
        is_active=True,
        olt_device_id=olt.id,
        config_method=ConfigMethod.omci,
        ip_protocol=IpProtocol.ipv4,
        wan_mode=WanMode.pppoe,
        wan_vlan_id=wan_vlan.id,
        pppoe_username="subscriber-user",
        pppoe_password="encrypted-secret",
        mgmt_ip_mode=MgmtIpMode.static_ip,
        mgmt_vlan_id=mgmt_vlan.id,
        mgmt_ip_address="10.10.10.2",
        wifi_ssid="subscriber-ssid",
        wifi_password="wifi-secret",
    )
    seed_legacy_profile_link(ont, bundle)
    db_session.add(ont)
    db_session.commit()
    ont.wifi_channel = "11"
    ont.wifi_security_mode = "WPA3-Personal"
    db_session.commit()

    plan = build_backfill_plan(db_session, ont)

    assert plan.outcome == "backfill"
    assert plan.bundle_id == str(bundle.id)
    assert plan.override_values == {
        "management.ip_mode": "static_ip",
        "management.ip_address": "10.10.10.2",
        "wan.pppoe_username": "subscriber-user",
        "wifi.ssid": "subscriber-ssid",
        "wifi.channel": "11",
        "wifi.security_mode": "WPA3-Personal",
    }
    assert "pppoe_password remains in legacy secret storage" in plan.warnings
    assert "wifi_password remains in legacy secret storage" in plan.warnings


def test_apply_backfill_plan_creates_assignment_and_overrides(db_session, region):
    olt = OLTDevice(name="OLT-Backfill-Apply", mgmt_ip="198.51.100.132", is_active=True)
    db_session.add(olt)
    db_session.flush()

    bundle = OntProvisioningProfile(
        name="Apply Bundle",
        olt_device_id=olt.id,
        is_active=True,
    )
    db_session.add(bundle)
    db_session.flush()

    ont = OntUnit(
        serial_number="BF-APPLY-001",
        is_active=True,
        olt_device_id=olt.id,
        pppoe_username="legacy-user",
    )
    seed_legacy_profile_link(ont, bundle)
    db_session.add(ont)
    db_session.commit()

    plan = build_backfill_plan(db_session, ont)
    assert plan.outcome == "backfill"

    apply_backfill_plan(db_session, ont, plan)
    db_session.commit()
    db_session.refresh(ont)

    assignment = db_session.scalars(
        select(OntBundleAssignment)
        .where(OntBundleAssignment.ont_unit_id == ont.id)
        .where(OntBundleAssignment.is_active.is_(True))
        .limit(1)
    ).first()
    overrides = db_session.scalars(
        select(OntConfigOverride).where(OntConfigOverride.ont_unit_id == ont.id)
    ).all()

    assert assignment is not None
    assert assignment.bundle_id == bundle.id
    assert assignment.status == OntBundleAssignmentStatus.applied
    assert ont.provisioning_profile_id is None
    assert len(overrides) == 1
    assert overrides[0].field_name == "wan.pppoe_username"
    assert overrides[0].value_json == {"value": "legacy-user"}
    assert overrides[0].source == OntConfigOverrideSource.workflow


def test_run_backfill_classifies_manual_review_and_unconfigured(db_session):
    manual_review_ont = OntUnit(
        serial_number="BF-MANUAL-001",
        is_active=True,
        pppoe_username="manual-user",
    )
    unconfigured_ont = OntUnit(
        serial_number="BF-UNCONFIG-001",
        is_active=True,
    )
    db_session.add_all([manual_review_ont, unconfigured_ont])
    db_session.commit()

    result = run_backfill(db_session)
    by_serial = {plan.serial_number: plan for plan in result.plans}

    assert by_serial["BF-MANUAL-001"].outcome == "manual_review"
    assert by_serial["BF-UNCONFIG-001"].outcome == "unconfigured"
    assert result.counts["manual_review"] >= 1
    assert result.counts["unconfigured"] >= 1


def test_build_backfill_plan_flags_cleared_projection_with_history_for_review(
    db_session,
):
    ont = OntUnit(
        serial_number="BF-PARTIAL-001",
        is_active=True,
        provisioning_status=OntProvisioningStatus.provisioned,
    )
    db_session.add(ont)
    db_session.commit()

    plan = build_backfill_plan(db_session, ont)

    assert plan.outcome == "manual_review"
    assert "provisioning history" in plan.reason
