from __future__ import annotations

from app.models.network import (
    OLTDevice,
    OltOntRegistration,
    OntAssignment,
    OntUnit,
    PonPort,
)
from app.services.network.ont_topology_reconcile import (
    reconcile_ont_pon_ports_from_registrations,
)


def test_reconcile_ont_pon_ports_dry_run_does_not_update(db_session):
    olt = OLTDevice(name="Topology OLT")
    wrong_pon = PonPort(olt=olt, name="0/2/7", port_number=7, is_active=True)
    ont = OntUnit(
        serial_number="HWTCRECON001",
        olt_device=olt,
        pon_port=wrong_pon,
        board="0/2",
        port="7",
        is_active=True,
    )
    assignment = OntAssignment(
        ont_unit=ont,
        pon_port=wrong_pon,
        active=True,
    )
    registration = OltOntRegistration(
        olt=olt,
        fsp="0/1/7",
        ont_id_on_olt=12,
        serial_number="HWTCRECON001",
        is_active=True,
    )
    db_session.add_all([olt, wrong_pon, ont, assignment, registration])
    db_session.commit()

    result = reconcile_ont_pon_ports_from_registrations(db_session)

    assert result.apply is False
    assert len(result.candidates) == 1
    assert result.candidates[0].current_fsp == "0/2/7"
    assert result.candidates[0].registration_fsp == "0/1/7"
    assert result.candidates[0].created_pon_port is True
    db_session.refresh(ont)
    db_session.refresh(assignment)
    assert ont.pon_port_id == wrong_pon.id
    assert assignment.pon_port_id == wrong_pon.id
    assert ont.board == "0/2"
    assert ont.port == "7"


def test_reconcile_ont_pon_ports_apply_updates_ont_and_assignment(db_session):
    olt = OLTDevice(name="Topology Apply OLT")
    wrong_pon = PonPort(olt=olt, name="0/4/3", port_number=3, is_active=True)
    target_pon = PonPort(olt=olt, name="0/2/3", port_number=3, is_active=True)
    ont = OntUnit(
        serial_number="HWTCRECON002",
        olt_device=olt,
        pon_port=wrong_pon,
        board="0/4",
        port="3",
        is_active=True,
    )
    assignment = OntAssignment(
        ont_unit=ont,
        pon_port=wrong_pon,
        active=True,
    )
    registration = OltOntRegistration(
        olt=olt,
        fsp="0/2/3",
        ont_id_on_olt=6,
        serial_number="HWTCRECON002",
        is_active=True,
    )
    db_session.add_all([olt, wrong_pon, target_pon, ont, assignment, registration])
    db_session.commit()

    result = reconcile_ont_pon_ports_from_registrations(db_session, apply=True)

    assert result.updated == 1
    assert result.created_pon_ports == 0
    assert result.candidates[0].changed is True
    db_session.refresh(ont)
    db_session.refresh(assignment)
    assert ont.pon_port_id == target_pon.id
    assert assignment.pon_port_id == target_pon.id
    assert ont.board == "0/2"
    assert ont.port == "3"


def test_reconcile_ont_pon_ports_matches_hex_registration_to_ascii_ont(db_session):
    olt = OLTDevice(name="Topology Canonical OLT")
    wrong_pon = PonPort(olt=olt, name="0/4/9", port_number=9, is_active=True)
    target_pon = PonPort(olt=olt, name="0/2/9", port_number=9, is_active=True)
    ont = OntUnit(
        serial_number="HWTC600AC29C",
        olt_device=olt,
        pon_port=wrong_pon,
        board="0/4",
        port="9",
        is_active=True,
    )
    assignment = OntAssignment(ont_unit=ont, pon_port=wrong_pon, active=True)
    registration = OltOntRegistration(
        olt=olt,
        fsp="0/2/9",
        ont_id_on_olt=9,
        serial_number="48575443600AC29C",
        is_active=True,
    )
    db_session.add_all([olt, wrong_pon, target_pon, ont, assignment, registration])
    db_session.commit()

    result = reconcile_ont_pon_ports_from_registrations(db_session, apply=True)

    assert result.updated == 1
    assert result.missing_from_db == 0
    db_session.refresh(ont)
    db_session.refresh(assignment)
    assert ont.pon_port_id == target_pon.id
    assert assignment.pon_port_id == target_pon.id
    assert ont.board == "0/2"
    assert ont.port == "9"


def test_reconcile_ont_pon_ports_apply_creates_missing_pon_port(db_session):
    olt = OLTDevice(name="Topology Create OLT")
    wrong_pon = PonPort(olt=olt, name="0/2/1", port_number=1, is_active=True)
    ont = OntUnit(
        serial_number="HWTCRECON003",
        olt_device=olt,
        pon_port=wrong_pon,
        board="0/2",
        port="1",
        is_active=True,
    )
    assignment = OntAssignment(ont_unit=ont, pon_port=wrong_pon, active=True)
    registration = OltOntRegistration(
        olt=olt,
        fsp="0/1/1",
        ont_id_on_olt=3,
        serial_number="HWTCRECON003",
        is_active=True,
    )
    db_session.add_all([olt, wrong_pon, ont, assignment, registration])
    db_session.commit()

    result = reconcile_ont_pon_ports_from_registrations(db_session, apply=True)

    assert result.updated == 1
    assert result.created_pon_ports == 1
    target_pon = (
        db_session.query(PonPort)
        .filter(PonPort.olt_id == olt.id, PonPort.name == "0/1/1")
        .one()
    )
    db_session.refresh(ont)
    db_session.refresh(assignment)
    assert target_pon.port_number == 1
    assert ont.pon_port_id == target_pon.id
    assert assignment.pon_port_id == target_pon.id


def test_reconcile_ont_pon_ports_counts_missing_and_invalid(db_session):
    olt = OLTDevice(name="Topology Count OLT")
    ont = OntUnit(
        serial_number="HWTCRECON004",
        olt_device=olt,
        board="0/1",
        port="1",
        is_active=True,
    )
    missing_db_registration = OltOntRegistration(
        olt=olt,
        fsp="0/1/2",
        ont_id_on_olt=2,
        serial_number="HWTCMISSINGDB",
        is_active=True,
    )
    invalid_registration = OltOntRegistration(
        olt=olt,
        fsp="bad-fsp",
        ont_id_on_olt=1,
        serial_number="HWTCRECON004",
        is_active=True,
    )
    db_session.add_all([olt, ont, missing_db_registration, invalid_registration])
    db_session.commit()

    result = reconcile_ont_pon_ports_from_registrations(db_session)

    assert result.missing_from_db == 1
    assert result.skipped == 1
    assert result.candidates[0].skipped_reason == "registration FSP is invalid"


def test_reconcile_ont_pon_ports_reports_missing_from_registration(db_session):
    olt = OLTDevice(name="Topology Missing OLT")
    ont = OntUnit(
        serial_number="HWTCMISSINGREG",
        olt_device=olt,
        board="0/1",
        port="1",
        is_active=True,
    )
    db_session.add_all([olt, ont])
    db_session.commit()

    result = reconcile_ont_pon_ports_from_registrations(db_session)

    assert result.missing_from_registration == 1
