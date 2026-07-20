from __future__ import annotations

import uuid

from app.models.network import (
    OLTDevice,
    OntAssignment,
    OntUnit,
    PonPort,
)
from app.services.network.olt_web_topology import repair_pon_ports_for_olt
from app.services.web_network_ont_assignments import resolve_pon_port_for_ont


def _olt(db_session, label: str) -> OLTDevice:
    suffix = uuid.uuid4().hex[:8]
    olt = OLTDevice(
        name=f"{label} {suffix}",
        hostname=f"{label.lower()}-{suffix}",
        is_active=True,
    )
    db_session.add(olt)
    db_session.flush()
    return olt


def test_inferred_pon_repair_reports_candidate_without_mutating(db_session):
    olt = _olt(db_session, "Repair")
    legacy = PonPort(olt_id=olt.id, name="legacy-port", port_number=3, is_active=True)
    ont = OntUnit(
        serial_number=f"REPAIR-{uuid.uuid4().hex[:10]}",
        olt_device_id=olt.id,
        pon_port=legacy,
        board="0/1",
        port="3",
        is_active=True,
    )
    db_session.add_all([legacy, ont])
    db_session.flush()
    assignment = OntAssignment(
        ont_unit_id=ont.id,
        pon_port_id=legacy.id,
        active=True,
    )
    db_session.add(assignment)
    db_session.commit()

    success, message, stats = repair_pon_ports_for_olt(db_session, str(olt.id))

    db_session.refresh(legacy)
    db_session.refresh(assignment)
    assert success is False
    assert "retired" in message
    assert stats["repaired"] == 0
    assert stats["merged"] == 0
    assert stats["unresolved"] == 1
    assert legacy.name == "legacy-port"
    assert legacy.is_active is True
    assert assignment.pon_port_id == legacy.id


def test_assignment_form_read_does_not_create_pon_inventory(db_session):
    olt = _olt(db_session, "Form")
    ont = OntUnit(
        serial_number=f"FORM-{uuid.uuid4().hex[:10]}",
        olt_device_id=olt.id,
        board="0/1",
        port="4",
        is_active=True,
    )
    db_session.add(ont)
    db_session.commit()

    result = resolve_pon_port_for_ont(db_session, ont)

    assert result["pon_port_resolved"] is False
    assert result["pon_port_label"] == "0/1/4"
    assert db_session.query(PonPort).count() == 0
