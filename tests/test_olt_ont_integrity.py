from fastapi import HTTPException

from app.schemas.network import (
    OltCardCreate,
    OltShelfCreate,
    OLTDeviceCreate,
    OntAssignmentCreate,
    OntUnitCreate,
    PonPortCreate,
)
from app.services.network.ont_serials import looks_synthetic_ont_serial
from app.services.network.ont_status_transitions import set_authorization_status
from app.services import network as network_service
from app.models.network import OntAuthorizationStatus, OntUnit


def test_manual_pon_port_create_rejects_card_from_other_olt(db_session):
    olt_a = network_service.olt_devices.create(
        db_session,
        OLTDeviceCreate(name="OLT A", hostname="olt-a.local"),
    )
    olt_b = network_service.olt_devices.create(
        db_session,
        OLTDeviceCreate(name="OLT B", hostname="olt-b.local"),
    )
    shelf_b = network_service.olt_shelves.create(
        db_session,
        OltShelfCreate(olt_id=olt_b.id, shelf_number=1),
    )
    card_b = network_service.olt_cards.create(
        db_session,
        OltCardCreate(shelf_id=shelf_b.id, slot_number=2),
    )

    try:
        network_service.pon_ports.create(
            db_session,
            PonPortCreate(card_id=card_b.id, port_number=1, olt_id=olt_a.id),
        )
    except HTTPException as exc:
        assert exc.status_code == 400
        assert exc.detail == "OLT card does not belong to the selected OLT"
    else:
        raise AssertionError("Expected HTTPException for mismatched OLT card")


def test_generated_snmp_ont_serials_are_synthetic():
    assert looks_synthetic_ont_serial("HW-86BF78E7-04104-2604111358482263")
    assert looks_synthetic_ont_serial("ZT-86BF78E7-04104")
    assert looks_synthetic_ont_serial("NK-86BF78E7-04104")
    assert looks_synthetic_ont_serial("OLT-86BF78E7-04104")
    assert not looks_synthetic_ont_serial("HWTC08D90492")


def test_ont_serial_can_be_reused_on_different_olts(db_session):
    olt_a = network_service.olt_devices.create(
        db_session,
        OLTDeviceCreate(name="Serial Scope OLT A", hostname="serial-scope-a.local"),
    )
    olt_b = network_service.olt_devices.create(
        db_session,
        OLTDeviceCreate(name="Serial Scope OLT B", hostname="serial-scope-b.local"),
    )

    ont_a = network_service.ont_units.create(
        db_session,
        OntUnitCreate(serial_number="HWTC-SCOPED-1", olt_device_id=olt_a.id),
    )
    ont_b = network_service.ont_units.create(
        db_session,
        OntUnitCreate(serial_number="HWTC-SCOPED-1", olt_device_id=olt_b.id),
    )

    assert ont_a.id != ont_b.id
    assert ont_a.serial_number == ont_b.serial_number


def test_inventory_ont_can_receive_active_assignment(db_session):
    olt = network_service.olt_devices.create(
        db_session,
        OLTDeviceCreate(
            name="Inventory Assignment OLT",
            hostname="inventory-assign.local",
        ),
    )
    pon = network_service.pon_ports.create(
        db_session,
        PonPortCreate(olt_id=olt.id, name="0/1/1"),
    )
    ont = network_service.ont_units.create(
        db_session,
        OntUnitCreate(
            serial_number="HWTC-INVENTORY-1",
            olt_device_id=olt.id,
            is_active=False,
        ),
    )

    assignment = network_service.ont_assignments.create(
        db_session,
        OntAssignmentCreate(ont_unit_id=ont.id, pon_port_id=pon.id, active=True),
    )

    assert assignment.active is True
    db_session.refresh(ont)
    assert ont.is_active is True


def test_olt_delete_rejects_active_onts(db_session):
    olt = network_service.olt_devices.create(
        db_session,
        OLTDeviceCreate(name="Delete Guard OLT", hostname="delete-guard.local"),
    )
    network_service.ont_units.create(
        db_session,
        OntUnitCreate(serial_number="HWTC-DELETE-GUARD", olt_device_id=olt.id),
    )

    try:
        network_service.olt_devices.delete(db_session, str(olt.id))
    except HTTPException as exc:
        assert exc.status_code == 409
        assert "Cannot delete OLT" in exc.detail
    else:
        raise AssertionError("Expected OLT delete to be blocked")


def test_pon_port_capacity_blocks_extra_active_assignment(db_session):
    olt = network_service.olt_devices.create(
        db_session,
        OLTDeviceCreate(name="Capacity OLT", hostname="capacity.local"),
    )
    pon = network_service.pon_ports.create(
        db_session,
        PonPortCreate(olt_id=olt.id, name="0/1/2", max_ont_capacity=1),
    )
    first = network_service.ont_units.create(
        db_session,
        OntUnitCreate(serial_number="HWTC-CAPACITY-1", olt_device_id=olt.id),
    )
    second = network_service.ont_units.create(
        db_session,
        OntUnitCreate(serial_number="HWTC-CAPACITY-2", olt_device_id=olt.id),
    )
    network_service.ont_assignments.create(
        db_session,
        OntAssignmentCreate(ont_unit_id=first.id, pon_port_id=pon.id, active=True),
    )

    try:
        network_service.ont_assignments.create(
            db_session,
            OntAssignmentCreate(ont_unit_id=second.id, pon_port_id=pon.id, active=True),
        )
    except HTTPException as exc:
        assert exc.status_code == 409
        assert "capacity" in exc.detail
    else:
        raise AssertionError("Expected PON port capacity conflict")


def test_ont_authorization_status_rejects_illegal_transition():
    ont = OntUnit(
        serial_number="HWTC-STATUS-1",
        authorization_status=OntAuthorizationStatus.authorized,
    )
    set_authorization_status(ont, OntAuthorizationStatus.pending)
    set_authorization_status(ont, OntAuthorizationStatus.authorized)

    new_ont = OntUnit(serial_number="HWTC-STATUS-2")
    try:
        set_authorization_status(new_ont, OntAuthorizationStatus.deauthorized)
    except ValueError as exc:
        assert "Illegal ONT authorization status transition" in str(exc)
    else:
        raise AssertionError("Expected illegal status transition to be rejected")
