from __future__ import annotations

from types import SimpleNamespace

from app.models.network import (
    ConfigMethod,
    IpProtocol,
    MgmtIpMode,
    OLTDevice,
    OntBundleAssignment,
    OntBundleAssignmentStatus,
    OntConfigOverride,
    OntProfileWanService,
    OntProvisioningProfile,
    OntUnit,
    OnuOnlineStatus,
    Vlan,
    WanConnectionType,
)
from app.services.network.effective_ont_config import resolve_effective_ont_config
from app.services.network.ont_profile_apply import apply_bundle_to_ont, detect_drift
from app.services.network.ont_profile_push import OntProfilePushService
from app.services.network.ont_provisioning_profiles import ont_provisioning_profiles
from app.services.network.provisioning_enforcement import ProvisioningEnforcement
from tests.legacy_ont_profile_link import seed_legacy_profile_link


def test_detect_gaps_and_counts_use_effective_bundle_values(db_session):
    olt = OLTDevice(
        name="OLT-GAPS",
        mgmt_ip="198.51.100.103",
        is_active=True,
        tr069_acs_server_id=None,
    )
    db_session.add(olt)
    db_session.flush()
    bundle = OntProvisioningProfile(
        name="Gap Bundle",
        olt_device_id=olt.id,
        mgmt_ip_mode=MgmtIpMode.static_ip,
        wifi_enabled=True,
        wifi_ssid_template="bundle-gap-ssid",
        is_active=True,
    )
    db_session.add(bundle)
    db_session.commit()

    db_session.add(
        OntProfileWanService(
            profile_id=bundle.id,
            name="Internet",
            s_vlan=905,
            connection_type=WanConnectionType.pppoe,
            pppoe_username_template="bundle-gap-user",
            is_active=True,
        )
    )
    ont = OntUnit(
        serial_number="ENF-GAPS-001",
        is_active=True,
        olt_device_id=olt.id,
        board="0/2",
        port="3",
        external_id="9",
        mgmt_ip_address="10.91.0.2",
        tr069_acs_server_id=None,
        observed_wan_ip=None,
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
    db_session.commit()

    gaps = ProvisioningEnforcement.detect_gaps(db_session, olt_id=str(olt.id))
    counts = ProvisioningEnforcement.detect_gap_counts(db_session, olt_id=str(olt.id))

    assert gaps["no_acs_on_olt"] == [str(ont.id)]
    assert gaps["wifi_pending_sync"] == []
    assert gaps["mgmt_pending_push"] == [str(ont.id)]
    assert counts["no_acs_on_olt"] == 1
    assert counts["mgmt_pending_push"] == 1


def test_effective_config_does_not_treat_legacy_profile_fk_as_bundle_assignment(
    db_session,
):
    bundle = OntProvisioningProfile(
        name="Legacy FK Only Bundle",
        mgmt_ip_mode=MgmtIpMode.static_ip,
        mgmt_vlan_tag=901,
        wifi_enabled=True,
        wifi_ssid_template="bundle-ssid",
        is_active=True,
    )
    db_session.add(bundle)
    db_session.flush()

    ont = OntUnit(
        serial_number="ENF-LEGACY-FK-001",
        is_active=True,
        mgmt_ip_mode=MgmtIpMode.dhcp,
        mgmt_ip_address="10.10.10.2",
        wifi_ssid="legacy-ssid",
    )
    seed_legacy_profile_link(ont, bundle)
    db_session.add(ont)
    db_session.commit()

    resolved = resolve_effective_ont_config(db_session, ont)

    assert resolved["bundle"] is None
    assert resolved["using_legacy_fallback"] is True
    assert resolved["values"]["mgmt_ip_mode"] == "dhcp"
    assert resolved["values"]["mgmt_ip_address"] == "10.10.10.2"
    assert resolved["values"]["wifi_ssid"] == "legacy-ssid"


def test_effective_config_resolves_bundle_templates_before_enforcement(db_session):
    bundle = OntProvisioningProfile(
        name="Template Bundle",
        wifi_ssid_template="DOTMAC-{serial_number}",
        is_active=True,
    )
    db_session.add(bundle)
    db_session.flush()
    db_session.add(
        OntProfileWanService(
            profile_id=bundle.id,
            name="Internet",
            connection_type=WanConnectionType.pppoe,
            pppoe_username_template="{serial_number}@isp.example",
            is_active=True,
        )
    )
    ont = OntUnit(serial_number="TPL-001", is_active=True)
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

    resolved = resolve_effective_ont_config(db_session, ont)

    assert resolved["config_ready"] is True
    assert resolved["values"]["pppoe_username"] == "TPL-001@isp.example"
    assert resolved["values"]["wifi_ssid"] == "DOTMAC-TPL-001"


def test_inactive_bundle_assignment_blocks_legacy_fallback(db_session):
    bundle = OntProvisioningProfile(name="Inactive Bundle", is_active=False)
    ont = OntUnit(
        serial_number="INACTIVE-BUNDLE-001",
        is_active=True,
        pppoe_username="legacy-user",
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

    resolved = resolve_effective_ont_config(db_session, ont)

    assert resolved["config_ready"] is False
    assert resolved["bundle_assignment_blocked_reason"] == "inactive_bundle"
    assert resolved["using_legacy_fallback"] is False
    assert resolved["values"]["pppoe_username"] is None


def test_detect_gaps_skips_non_applied_bundle_assignments(db_session):
    olt = OLTDevice(
        name="OLT-NON-APPLIED",
        mgmt_ip="198.51.100.201",
        is_active=True,
        tr069_acs_server_id=None,
    )
    bundle = OntProvisioningProfile(
        name="Planned Bundle",
        olt_device_id=olt.id,
        is_active=True,
    )
    db_session.add_all([olt, bundle])
    db_session.flush()
    db_session.add(
        OntProfileWanService(
            profile_id=bundle.id,
            name="Internet",
            connection_type=WanConnectionType.pppoe,
            pppoe_username_template="planned-user",
            is_active=True,
        )
    )
    ont = OntUnit(serial_number="PLANNED-001", is_active=True, olt_device_id=olt.id)
    db_session.add(ont)
    db_session.flush()
    db_session.add(
        OntBundleAssignment(
            ont_unit_id=ont.id,
            bundle_id=bundle.id,
            status=OntBundleAssignmentStatus.planned,
            is_active=True,
        )
    )
    db_session.commit()

    gaps = ProvisioningEnforcement.detect_gaps(db_session, olt_id=str(olt.id))

    assert all(str(ont.id) not in ont_ids for ont_ids in gaps.values())


def test_apply_bundle_to_ont_rejects_inactive_bundle(db_session):
    bundle = OntProvisioningProfile(name="Do Not Apply", is_active=False)
    ont = OntUnit(serial_number="REJECT-INACTIVE-001", is_active=True)
    db_session.add_all([bundle, ont])
    db_session.commit()

    result = apply_bundle_to_ont(db_session, str(ont.id), str(bundle.id))

    assert result.success is False
    assert "inactive" in result.message
    assert resolve_effective_ont_config(db_session, ont)["bundle_assignment"] is None


def test_count_onts_by_profile_uses_active_bundle_assignments_only(db_session):
    active_bundle = OntProvisioningProfile(name="Active Count Bundle", is_active=True)
    legacy_only_bundle = OntProvisioningProfile(name="Legacy Count Bundle", is_active=True)
    db_session.add_all([active_bundle, legacy_only_bundle])
    db_session.flush()

    assigned_ont = OntUnit(
        serial_number="COUNT-ACTIVE-ASSIGNMENT-001",
        is_active=True,
    )
    legacy_only_ont = OntUnit(
        serial_number="COUNT-LEGACY-ONLY-001",
        is_active=True,
    )
    inactive_assignment_ont = OntUnit(
        serial_number="COUNT-INACTIVE-ASSIGNMENT-001",
        is_active=True,
    )
    seed_legacy_profile_link(assigned_ont, legacy_only_bundle)
    seed_legacy_profile_link(legacy_only_ont, legacy_only_bundle)
    seed_legacy_profile_link(inactive_assignment_ont, active_bundle)
    db_session.add_all([assigned_ont, legacy_only_ont, inactive_assignment_ont])
    db_session.flush()

    db_session.add(
        OntBundleAssignment(
            ont_unit_id=assigned_ont.id,
            bundle_id=active_bundle.id,
            status=OntBundleAssignmentStatus.applied,
            is_active=True,
        )
    )
    db_session.add(
        OntBundleAssignment(
            ont_unit_id=inactive_assignment_ont.id,
            bundle_id=active_bundle.id,
            status=OntBundleAssignmentStatus.applied,
            is_active=False,
        )
    )
    db_session.commit()

    counts = ont_provisioning_profiles.count_onts_by_profile(db_session)

    assert counts == {str(active_bundle.id): 1}


def test_profile_push_requires_active_bundle_assignment_not_legacy_profile_fk(
    db_session,
):
    bundle = OntProvisioningProfile(name="Legacy Push Bundle", is_active=True)
    db_session.add(bundle)
    db_session.flush()

    ont = OntUnit(
        serial_number="PROFILE-PUSH-LEGACY-FK-001",
        is_active=True,
    )
    seed_legacy_profile_link(ont, bundle)
    db_session.add(ont)
    db_session.commit()

    result = OntProfilePushService.push_profile_to_device(db_session, str(ont.id))

    assert result.success is False
    assert result.message == "ONT has no active configuration bundle"


def test_enforce_pppoe_push_uses_effective_override_username(db_session, monkeypatch):
    olt = OLTDevice(name="OLT-PPPOE", mgmt_ip="198.51.100.101", is_active=True)
    db_session.add(olt)
    db_session.flush()
    bundle = OntProvisioningProfile(name="Bundle", olt_device_id=olt.id, is_active=True)
    db_session.add(bundle)
    db_session.commit()

    db_session.add(
        OntProfileWanService(
            profile_id=bundle.id,
            name="Internet",
            s_vlan=900,
            connection_type=WanConnectionType.pppoe,
            pppoe_username_template="bundle-user",
            is_active=True,
        )
    )
    ont = OntUnit(
        serial_number="ENF-PPPOE-001",
        is_active=True,
        olt_device_id=olt.id,
        pppoe_username="legacy-user",
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
            value_json={"value": "override-user"},
        )
    )
    db_session.commit()

    calls: list[tuple[str, str, str]] = []

    class FakeAcsWriter:
        def set_pppoe_credentials(self, db, ont_id, username, password):
            calls.append((str(ont_id), str(username), str(password)))
            return SimpleNamespace(success=True, message="ok")

    monkeypatch.setattr(
        "app.services.network.provisioning_enforcement._acs_config_writer",
        lambda: FakeAcsWriter(),
    )

    class FakeCreds:
        def get_by_username(self, username):
            assert username == "override-user"
            return SimpleNamespace(secret_hash=None)

    monkeypatch.setattr(
        "app.services.network.provisioning_enforcement._resolve_access_credential_password",
        lambda db, credentials, ont, username=None: "resolved-pass",
    )

    result = ProvisioningEnforcement.enforce_pppoe_push(
        db_session,
        [str(ont.id)],
        credentials=FakeCreds(),
    )

    assert result == {"pushed": 1, "failed": 0, "skipped": 0}
    assert calls == [(str(ont.id), "override-user", "resolved-pass")]


def test_resolve_access_credential_password_uses_effective_username_fallback(
    db_session, monkeypatch
):
    from app.models.network import (
        OntBundleAssignment,
        OntBundleAssignmentStatus,
        OntConfigOverride,
        OntProvisioningProfile,
        OntUnit,
    )
    from app.services.network.provisioning_enforcement import (
        _resolve_access_credential_password,
    )

    bundle = OntProvisioningProfile(name="Cred Bundle", is_active=True)
    ont = OntUnit(serial_number="ENF-CRED-001", is_active=True, pppoe_username=None)
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
    db_session.add(
        OntConfigOverride(
            ont_unit_id=ont.id,
            field_name="wan.pppoe_username",
            value_json={"value": "effective-user"},
        )
    )
    db_session.commit()

    class FakeCreds:
        def get_by_username(self, username):
            assert username == "effective-user"
            return SimpleNamespace(secret_hash="secret-hash")

    monkeypatch.setattr(
        "app.services.credential_crypto.decrypt_credential",
        lambda value: "resolved-secret",
    )

    password = _resolve_access_credential_password(db_session, FakeCreds(), ont)

    assert password == "resolved-secret"


def test_enforce_wifi_and_management_use_effective_bundle_values(
    db_session, region, monkeypatch
):
    olt = OLTDevice(name="OLT-WIFI", mgmt_ip="198.51.100.102", is_active=True)
    db_session.add(olt)
    db_session.flush()
    mgmt_vlan = Vlan(
        tag=901,
        name="Mgmt",
        region_id=region.id,
        olt_device_id=olt.id,
        is_active=True,
    )
    bundle = OntProvisioningProfile(
        name="Wifi Bundle",
        olt_device_id=olt.id,
        mgmt_ip_mode=MgmtIpMode.static_ip,
        mgmt_vlan_tag=901,
        wifi_enabled=True,
        wifi_ssid_template="bundle-ssid",
        wifi_channel="11",
        wifi_security_mode="WPA2-Personal",
        is_active=True,
    )
    db_session.add_all([mgmt_vlan, bundle])
    db_session.commit()

    ont = OntUnit(
        serial_number="ENF-WIFI-001",
        is_active=True,
        olt_device_id=olt.id,
        board="0/1",
        port="1",
        external_id="7",
        mgmt_ip_address="10.90.0.2",
        wifi_ssid="legacy-ssid",
        effective_status=OnuOnlineStatus.online,
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
    db_session.commit()

    wifi_calls: list[dict[str, object]] = []
    iphost_calls: list[dict[str, object]] = []

    class FakeAcsWriter:
        def set_wifi_config(self, db, ont_id, **kwargs):
            wifi_calls.append({"ont_id": str(ont_id), **kwargs})
            return SimpleNamespace(success=True, message="ok")

    class FakeAdapter:
        def configure_iphost(self, fsp, ont_id_on_olt, **kwargs):
            iphost_calls.append(
                {"fsp": fsp, "ont_id_on_olt": ont_id_on_olt, **kwargs}
            )
            return SimpleNamespace(success=True, message="ok")

    monkeypatch.setattr(
        "app.services.network.provisioning_enforcement._acs_config_writer",
        lambda: FakeAcsWriter(),
    )
    monkeypatch.setattr(
        "app.services.network.olt_protocol_adapters.get_protocol_adapter",
        lambda olt: FakeAdapter(),
    )
    monkeypatch.setattr(
        "app.services.network.serial_utils.parse_ont_id_on_olt",
        lambda raw: 7,
    )
    monkeypatch.setattr(
        "app.services.credential_crypto.decrypt_credential",
        lambda value: value,
    )

    wifi_result = ProvisioningEnforcement.enforce_wifi_push(db_session, [str(ont.id)])
    mgmt_result = ProvisioningEnforcement.enforce_management_config(
        db_session, [str(ont.id)]
    )

    assert wifi_result == {"pushed": 1, "failed": 0, "skipped": 0}
    assert wifi_calls == [
        {
            "ont_id": str(ont.id),
            "enabled": True,
            "ssid": "bundle-ssid",
            "password": None,
            "channel": "11",
            "security_mode": "WPA2-Personal",
        }
    ]

    assert mgmt_result == {"pushed": 1, "failed": 0, "skipped": 0}
    assert iphost_calls == [
        {
            "fsp": "0/1/1",
            "ont_id_on_olt": 7,
            "vlan": 901,
            "mode": "static",
            "ip_address": "10.90.0.2",
            "subnet_mask": "255.255.255.0",
            "gateway": "10.90.0.1",
        }
    ]


def test_detect_drift_uses_effective_bundle_values_not_cleared_projection(db_session):
    olt = OLTDevice(name="OLT-DRIFT", mgmt_ip="198.51.100.111", is_active=True)
    db_session.add(olt)
    db_session.flush()

    bundle = OntProvisioningProfile(
        name="Drift Bundle",
        olt_device_id=olt.id,
        config_method=ConfigMethod.tr069,
        ip_protocol=IpProtocol.ipv4,
        mgmt_ip_mode=MgmtIpMode.dhcp,
        is_active=True,
    )
    db_session.add(bundle)
    db_session.flush()

    ont = OntUnit(
        serial_number="DRIFT-EFFECTIVE-001",
        is_active=True,
        olt_device_id=olt.id,
        config_method=None,
        ip_protocol=None,
        mgmt_ip_mode=None,
    )
    seed_legacy_profile_link(ont, bundle)
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

    report = detect_drift(db_session, str(ont.id))

    assert report is not None
    assert report.has_drift is False
    assert report.drifted_fields == []
