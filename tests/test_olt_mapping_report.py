from __future__ import annotations

from datetime import UTC, datetime

from app.models.network import (
    OLTDevice,
    OltLineProfile,
    OltOntRegistration,
    OltOnuTypeProfileMapping,
    OltServiceProfile,
    OntUnit,
)
from app.services.network.olt_mapping_report import build_olt_mapping_coverage_report


def test_mapping_report_lists_unmapped_inventory_and_imported_equipment(db_session):
    olt = OLTDevice(name="Mapping Report OLT", is_active=True)
    db_session.add(olt)
    db_session.flush()
    imported_at = datetime.now(UTC)
    db_session.add_all(
        [
            OltLineProfile(
                olt_id=olt.id,
                profile_id=40,
                name="LINE",
                last_imported_at=imported_at,
            ),
            OltServiceProfile(
                olt_id=olt.id,
                profile_id=41,
                name="EG8145V5",
                last_imported_at=imported_at,
            ),
        ]
    )
    db_session.flush()
    db_session.add_all(
        [
            OltOnuTypeProfileMapping(
                olt_id=olt.id,
                equipment_id="EG8145V5",
                line_profile_id=40,
                service_profile_id=41,
            ),
            OntUnit(
                serial_number="MAP-INV-1",
                olt_device_id=olt.id,
                model="EG8145V5",
                is_active=True,
            ),
            OntUnit(
                serial_number="MAP-INV-2",
                olt_device_id=olt.id,
                model="HG8546M",
                is_active=True,
            ),
            OltOntRegistration(
                olt_id=olt.id,
                fsp="0/1/0",
                ont_id_on_olt=1,
                serial_number="MAP-REG-1",
                equipment_id="HG8546M",
                is_active=True,
                last_imported_at=imported_at,
            ),
        ]
    )
    db_session.flush()

    report = build_olt_mapping_coverage_report(db_session, olt_id=str(olt.id))

    assert len(report) == 1
    assert report[0].mapped_equipment_count == 1
    assert report[0].observed_equipment_count == 2
    assert report[0].missing_count == 1
    missing = report[0].missing[0]
    assert missing.equipment_id == "HG8546M"
    assert missing.inventory_count == 1
    assert missing.imported_registration_count == 1
    assert missing.total_count == 2


def test_mapping_report_uses_onu_type_name_when_ont_model_is_missing(db_session):
    from app.models.network import OnuType

    olt = OLTDevice(name="ONU Type Mapping OLT", is_active=True)
    onu_type = OnuType(name="HS8546V5")
    db_session.add_all([olt, onu_type])
    db_session.flush()
    db_session.add(
        OntUnit(
            serial_number="MAP-ONU-TYPE",
            olt_device_id=olt.id,
            onu_type_id=onu_type.id,
            is_active=True,
        )
    )
    db_session.flush()

    report = build_olt_mapping_coverage_report(db_session, olt_id=str(olt.id))

    assert report[0].missing_count == 1
    assert report[0].missing[0].equipment_id == "HS8546V5"
