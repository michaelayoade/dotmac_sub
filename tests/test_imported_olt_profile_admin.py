"""Tests for imported OLT profile admin helpers."""

from app.models.network import (
    OLTDevice,
    OltLineProfile,
    OltOnuTypeProfileMapping,
    OltServiceProfile,
)
from app.services import web_network_olt_profiles


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
