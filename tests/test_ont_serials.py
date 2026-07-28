from app.models.network import OLTDevice, OntUnit
from app.services.network.ont_serials import (
    build_active_ont_serial_index,
    find_unique_active_ont_by_serial,
)


def test_find_unique_active_ont_by_serial_matches_huawei_hex_variant(db_session):
    ont = OntUnit(serial_number="HWTC7D4701C3", is_active=True)
    db_session.add(ont)
    db_session.commit()

    assert find_unique_active_ont_by_serial(db_session, "485754437D4701C3") == ont


def test_find_unique_active_ont_by_serial_refuses_ambiguous_duplicates(db_session):
    olt_one = OLTDevice(name="Serial OLT 1", mgmt_ip="192.0.2.11")
    olt_two = OLTDevice(name="Serial OLT 2", mgmt_ip="192.0.2.12")
    db_session.add_all([olt_one, olt_two])
    db_session.flush()
    db_session.add_all(
        [
            OntUnit(
                serial_number="HWTC7D4701C3",
                olt_device_id=olt_one.id,
                is_active=True,
            ),
            OntUnit(
                serial_number="485754437D4701C3",
                olt_device_id=olt_two.id,
                is_active=True,
            ),
        ]
    )
    db_session.commit()

    assert find_unique_active_ont_by_serial(db_session, "HWTC7D4701C3") is None


def test_find_unique_active_ont_by_serial_ignores_synthetic_serials(db_session):
    db_session.add(
        OntUnit(
            serial_number="HW-86BF78E7-04104-2604111358482263",
            is_active=True,
        )
    )
    db_session.commit()

    assert (
        find_unique_active_ont_by_serial(
            db_session,
            "HW-86BF78E7-04104-2604111358482263",
        )
        is None
    )


def test_active_ont_serial_index_preserves_unique_and_ambiguous_matching(db_session):
    ont = OntUnit(serial_number="HWTC7D4701C3", is_active=True)
    db_session.add(ont)
    db_session.commit()

    index = build_active_ont_serial_index(db_session)
    assert index.get(ont.id) == ont
    assert index.find_unique("485754437D4701C3") == ont

    duplicate = OntUnit(serial_number="485754437D4701C3", is_active=True)
    db_session.add(duplicate)
    db_session.commit()

    index = build_active_ont_serial_index(db_session)

    assert index.find_unique("HWTC7D4701C3") is None
    assert index.find_unique("HWTC7D4701C3", exclude_ont_id=duplicate.id) == ont
