"""Tests for imported OLT profile admin helpers."""

from datetime import UTC, datetime

from app.models.network import (
    OLTDevice,
    OltLineProfileGemMapping,
    OltLineProfile,
    OltOntRegistration,
    OltOnuTypeProfileMapping,
    OltServiceProfile,
    OltServicePort,
)
from app.services import web_network_olt_profiles
from app.services.network.olt_state_import import (
    _import_line_profile_gem_mappings_from_config,
    _import_observed_service_ports_from_config,
    _import_service_port_gem_mappings_from_config,
)


def test_imported_profile_state_context_returns_db_profiles(db_session):
    olt = OLTDevice(name="Imported Profiles OLT", vendor="Huawei")
    db_session.add(olt)
    db_session.flush()
    db_session.add_all(
        [
            OltLineProfile(olt_id=olt.id, profile_id=40, name="LINE"),
            OltServiceProfile(olt_id=olt.id, profile_id=41, name="EG8145V5"),
        ]
    )
    db_session.flush()
    db_session.add(
        OltLineProfileGemMapping(
            olt_id=olt.id,
            line_profile_id=40,
            source="service_port",
            source_key="service-port:vlan:203:gem:1",
            gem_index=1,
            vlan_id=203,
            usage_count=10,
        )
    )
    db_session.flush()
    db_session.add(
        OltOnuTypeProfileMapping(
            olt_id=olt.id,
            equipment_id="EG8145V5",
            line_profile_id=40,
            service_profile_id=41,
        )
    )
    db_session.flush()

    context = web_network_olt_profiles.imported_profile_state_context(
        db_session,
        str(olt.id),
    )

    assert context["error"] is None
    assert [profile.profile_id for profile in context["line_profiles"]] == [40]
    assert [profile.profile_id for profile in context["service_profiles"]] == [41]
    assert [mapping.equipment_id for mapping in context["profile_mappings"]] == [
        "EG8145V5"
    ]
    assert [mapping.gem_index for mapping in context["gem_mappings"]] == [1]


def test_save_imported_profile_mapping_requires_imported_profiles(db_session):
    olt = OLTDevice(name="Mapping Requires Profiles", vendor="Huawei")
    db_session.add(olt)
    db_session.flush()

    ok, message = web_network_olt_profiles.save_imported_profile_mapping(
        db_session,
        str(olt.id),
        equipment_id="EG8145V5",
        line_profile_id=40,
        service_profile_id=41,
    )

    assert ok is False
    assert "Line profile 40 has not been imported" in message


def test_save_imported_profile_mapping_upserts_explicit_mapping(db_session):
    olt = OLTDevice(name="Mapping Upsert OLT", vendor="Huawei")
    db_session.add(olt)
    db_session.flush()
    db_session.add_all(
        [
            OltLineProfile(olt_id=olt.id, profile_id=40, name="LINE"),
            OltLineProfile(olt_id=olt.id, profile_id=50, name="LINE2"),
            OltServiceProfile(olt_id=olt.id, profile_id=41, name="EG8145V5"),
            OltServiceProfile(olt_id=olt.id, profile_id=51, name="EG8145V5-ALT"),
        ]
    )
    db_session.flush()

    ok, message = web_network_olt_profiles.save_imported_profile_mapping(
        db_session,
        str(olt.id),
        equipment_id="EG8145V5",
        line_profile_id=40,
        service_profile_id=41,
    )
    assert ok is True
    assert "Created mapping" in message

    ok, message = web_network_olt_profiles.save_imported_profile_mapping(
        db_session,
        str(olt.id),
        equipment_id="EG8145V5",
        line_profile_id=50,
        service_profile_id=51,
    )
    assert ok is True
    assert "Updated mapping" in message

    mappings = web_network_olt_profiles.imported_profile_state_context(
        db_session,
        str(olt.id),
    )["profile_mappings"]
    assert len(mappings) == 1
    assert mappings[0].line_profile_id == 50
    assert mappings[0].service_profile_id == 51


def test_delete_imported_profile_mapping_scopes_to_olt(db_session):
    olt = OLTDevice(name="Mapping Delete OLT", vendor="Huawei")
    other_olt = OLTDevice(name="Other OLT", vendor="Huawei")
    db_session.add_all([olt, other_olt])
    db_session.flush()
    db_session.add_all(
        [
            OltLineProfile(olt_id=olt.id, profile_id=40, name="LINE"),
            OltServiceProfile(olt_id=olt.id, profile_id=41, name="EG8145V5"),
        ]
    )
    db_session.flush()
    db_session.add(
        OltOnuTypeProfileMapping(
            olt_id=olt.id,
            equipment_id="EG8145V5",
            line_profile_id=40,
            service_profile_id=41,
        )
    )
    db_session.flush()
    mapping = web_network_olt_profiles.imported_profile_state_context(
        db_session,
        str(olt.id),
    )["profile_mappings"][0]

    ok, message = web_network_olt_profiles.delete_imported_profile_mapping(
        db_session,
        str(other_olt.id),
        str(mapping.id),
    )
    assert ok is False
    assert message == "Mapping not found"

    ok, message = web_network_olt_profiles.delete_imported_profile_mapping(
        db_session,
        str(olt.id),
        str(mapping.id),
    )
    assert ok is True
    assert "Deleted mapping" in message


def test_import_gem_mappings_from_lineprofile_and_service_ports(db_session):
    olt = OLTDevice(name="GEM Import OLT", vendor="Huawei")
    db_session.add(olt)
    db_session.flush()
    db_session.add(
        OltLineProfile(olt_id=olt.id, profile_id=40, name="SMARTOLT_FLEXIBLE_GPON")
    )
    db_session.flush()
    config = """
ont-lineprofile gpon profile-id 40 profile-name "SMARTOLT_FLEXIBLE_GPON"
 gem add 1 eth tcont 1
 gem add 2 eth tcont 2
 gem mapping 1 1 priority 0
 gem mapping 2 1 vlan 201
 commit
 quit
interface gpon 0/1
 ont add 7 5 sn-auth "48575443348F8A84" omci ont-lineprofile-id 40 ont-srvprofile-id 41
 quit
service-port 10 vlan 203 gpon 0/1/7 ont 5 gemport 1 multi-service user-vlan 203 tag-transform translate
service-port 11 vlan 201 gpon 0/1/7 ont 5 gemport 2 multi-service user-vlan 201 tag-transform translate
"""
    imported_at = datetime.now(UTC)
    line_count = _import_line_profile_gem_mappings_from_config(
        db_session,
        olt,
        config,
        imported_at,
    )
    db_session.add(
        OltOntRegistration(
            olt_id=olt.id,
            fsp="0/1/7",
            ont_id_on_olt=5,
            line_profile_id=40,
            service_profile_id=None,
            is_active=True,
        )
    )
    db_session.flush()
    service_count = _import_service_port_gem_mappings_from_config(
        db_session,
        olt,
        config,
        imported_at,
    )
    observed_count = _import_observed_service_ports_from_config(
        db_session,
        olt,
        config,
        imported_at,
    )
    db_session.flush()

    context = web_network_olt_profiles.imported_profile_state_context(
        db_session,
        str(olt.id),
    )
    rows = {
        (row.source, row.vlan_id, row.priority, row.gem_index)
        for row in context["gem_mappings"]
    }
    assert line_count == 2
    assert service_count == 2
    assert observed_count == 2
    observed_ports = {
        (row.port_index, row.vlan_id, row.gem_index)
        for row in db_session.query(OltServicePort).all()
    }
    assert (10, 203, 1) in observed_ports
    assert (11, 201, 2) in observed_ports
    assert ("line_profile", None, 0, 1) in rows
    assert ("line_profile", 201, None, 2) in rows
    assert ("service_port", 203, None, 1) in rows
    assert ("service_port", 201, None, 2) in rows
