from __future__ import annotations

from app.models.network import (
    OLTDevice,
    OltLineProfile,
    OltOntRegistration,
    OltServicePort,
    OltServiceProfile,
)
from app.services.network.olt_service_port_gaps import find_missing_service_ports


def test_find_missing_service_ports_reports_registration_without_binding(db_session):
    olt = OLTDevice(name="Service Port Gap OLT")
    db_session.add(olt)
    db_session.flush()
    db_session.add_all(
        [
            OltLineProfile(olt_id=olt.id, profile_id=40, name="LINE"),
            OltServiceProfile(olt_id=olt.id, profile_id=41, name="SERVICE"),
        ]
    )
    db_session.flush()
    db_session.add_all(
        [
            OltOntRegistration(
                olt_id=olt.id,
                fsp="0/1/1",
                ont_id_on_olt=1,
                serial_number="HWTCGAP00001",
                line_profile_id=40,
                service_profile_id=41,
                is_active=True,
            ),
            OltOntRegistration(
                olt_id=olt.id,
                fsp="0/1/2",
                ont_id_on_olt=2,
                serial_number="HWTCBOUND001",
                is_active=True,
            ),
            OltServicePort(
                olt_device_id=olt.id,
                port_index=100,
                fsp="0/1/2",
                ont_id_on_olt=2,
                vlan_id=203,
                gem_index=1,
                source="running_config",
            ),
        ]
    )
    db_session.commit()

    missing = find_missing_service_ports(db_session)

    assert len(missing) == 1
    assert missing[0].serial_number == "HWTCGAP00001"
    assert missing[0].fsp == "0/1/1"
    assert missing[0].ont_id_on_olt == 1
    assert missing[0].line_profile_id == 40
    assert missing[0].service_profile_id == 41


def test_find_missing_service_ports_scopes_to_olt(db_session):
    olt = OLTDevice(name="Scoped Service Port Gap OLT")
    other_olt = OLTDevice(name="Other Service Port Gap OLT")
    db_session.add_all([olt, other_olt])
    db_session.flush()
    db_session.add_all(
        [
            OltOntRegistration(
                olt_id=olt.id,
                fsp="0/1/1",
                ont_id_on_olt=1,
                serial_number="HWTCINCLUDED",
                is_active=True,
            ),
            OltOntRegistration(
                olt_id=other_olt.id,
                fsp="0/1/1",
                ont_id_on_olt=1,
                serial_number="HWTCEXCLUDED",
                is_active=True,
            ),
        ]
    )
    db_session.commit()

    missing = find_missing_service_ports(db_session, olt_id=str(olt.id))

    assert [item.serial_number for item in missing] == ["HWTCINCLUDED"]
