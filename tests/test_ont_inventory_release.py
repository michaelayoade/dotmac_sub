from __future__ import annotations

import uuid
from datetime import UTC

import pytest

from app.models.network import OLTDevice, OntAssignment, OntUnit, PonPort
from app.services.network.ont_inventory_release import (
    release_ont_electronic_identity,
)


def test_inventory_release_clears_exact_customer_and_topology_identity(
    db_session,
    subscription,
):
    olt = OLTDevice(
        name=f"Release OLT {uuid.uuid4().hex[:8]}",
        hostname=f"release-{uuid.uuid4().hex[:8]}",
        is_active=True,
    )
    db_session.add(olt)
    db_session.flush()
    pon = PonPort(olt_id=olt.id, name="0/1/2", is_active=True)
    db_session.add(pon)
    db_session.flush()
    ont = OntUnit(
        serial_number=f"RELEASE-{uuid.uuid4().hex[:12]}",
        olt_device_id=olt.id,
        pon_port_id=pon.id,
        board="0/1",
        port="2",
        external_id="0/1/2:7",
        is_active=True,
    )
    db_session.add(ont)
    db_session.flush()
    assignment = OntAssignment(
        ont_unit_id=ont.id,
        subscription_id=subscription.id,
        subscriber_id=subscription.subscriber_id,
        pon_port_id=pon.id,
        pppoe_username="retired-user",
        active=True,
    )
    db_session.add(assignment)
    db_session.commit()

    result = release_ont_electronic_identity(
        db_session,
        ont_unit_id=ont.id,
    )
    db_session.commit()
    db_session.refresh(ont)
    db_session.refresh(assignment)

    assert result.ont_unit_id == ont.id
    assert result.assignment_ids == (assignment.id,)
    assert result.deactivated_assignment_ids == (assignment.id,)
    assert assignment.active is False
    assert assignment.released_at is not None
    assert assignment.released_at.replace(tzinfo=UTC) == result.released_at
    assert assignment.release_reason == "returned_to_inventory"
    assert assignment.subscription_id is None
    assert assignment.subscriber_id is None
    assert assignment.service_address_id is None
    assert assignment.pon_port_id is None
    assert assignment.pppoe_username == "retired-user"
    assert ont.olt_device_id is None
    assert ont.pon_port_id is None
    assert ont.board is None
    assert ont.port is None
    assert ont.external_id is None


def test_inventory_release_replay_is_idempotent(db_session):
    ont = OntUnit(
        serial_number=f"REPLAY-{uuid.uuid4().hex[:12]}",
        is_active=True,
    )
    db_session.add(ont)
    db_session.flush()
    assignment = OntAssignment(ont_unit_id=ont.id, active=False)
    db_session.add(assignment)
    db_session.commit()

    first = release_ont_electronic_identity(db_session, ont_unit_id=str(ont.id))
    second = release_ont_electronic_identity(db_session, ont_unit_id=ont.id)

    assert first.assignment_ids == (assignment.id,)
    assert first.deactivated_assignment_ids == ()
    assert second.assignment_ids == (assignment.id,)
    assert second.deactivated_assignment_ids == ()


def test_inventory_release_rejects_unknown_ont(db_session):
    with pytest.raises(ValueError, match="ONT not found"):
        release_ont_electronic_identity(
            db_session,
            ont_unit_id=uuid.uuid4(),
        )
