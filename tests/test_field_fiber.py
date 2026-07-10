from __future__ import annotations

from datetime import UTC, datetime
from uuid import uuid4

import pytest
from fastapi import HTTPException

from app.models.dispatch import TechnicianProfile
from app.models.fiber_change_request import FiberChangeRequest
from app.models.field_fiber import FieldFiberTestResult
from app.models.network import (
    FiberAccessPoint,
    FiberSpliceClosure,
    FiberSpliceTray,
    FiberStrand,
    FiberStrandStatus,
)
from app.models.subscriber import Subscriber, UserType
from app.models.system_user import SystemUser
from app.models.work_order_mirror import WorkOrderMirror
from app.services.field import fiber as field_fiber


def _user(db_session) -> SystemUser:
    user = SystemUser(
        first_name="Fiber",
        last_name="Tech",
        display_name="Fiber Tech",
        email=f"fiber-tech-{uuid4().hex[:8]}@example.com",
        user_type=UserType.system_user,
    )
    db_session.add(user)
    db_session.flush()
    return user


def _auth(user: SystemUser) -> dict:
    return {
        "principal_id": str(user.id),
        "person_id": str(user.id),
        "subscriber_id": str(user.id),
        "principal_type": "system_user",
        "roles": [],
        "scopes": [],
    }


def _profile(db_session, user: SystemUser) -> TechnicianProfile:
    profile = TechnicianProfile(
        person_id=user.id,
        system_user_id=user.id,
        crm_person_id="crm-fiber-tech",
    )
    db_session.add(profile)
    db_session.flush()
    return profile


def _subscriber(db_session) -> Subscriber:
    subscriber = Subscriber(
        first_name="Fiber",
        last_name="Customer",
        email=f"fiber-customer-{uuid4().hex[:8]}@example.com",
    )
    db_session.add(subscriber)
    db_session.flush()
    return subscriber


def _work_order(db_session, subscriber: Subscriber) -> WorkOrderMirror:
    row = WorkOrderMirror(
        crm_work_order_id="wo-fiber",
        subscriber_id=subscriber.id,
        title="Fiber repair",
        status="in_progress",
        assigned_to_crm_person_id="crm-fiber-tech",
        scheduled_start=datetime.now(UTC),
    )
    db_session.add(row)
    db_session.flush()
    return row


def _plant(db_session):
    closure = FiberSpliceClosure(name="Closure A", is_active=True)
    db_session.add(closure)
    db_session.flush()
    tray = FiberSpliceTray(closure_id=closure.id, tray_number=1)
    strand_a = FiberStrand(
        cable_name="Cable A",
        strand_number=1,
        status=FiberStrandStatus.available,
        is_active=True,
    )
    strand_b = FiberStrand(
        cable_name="Cable A",
        strand_number=2,
        status=FiberStrandStatus.reserved,
        is_active=True,
    )
    access_point = FiberAccessPoint(name="FAP A", is_active=True)
    db_session.add_all([tray, strand_a, strand_b, access_point])
    db_session.flush()
    return closure, tray, strand_a, strand_b, access_point


def test_propose_splice_creates_pending_change_request(db_session):
    user = _user(db_session)
    _profile(db_session, user)
    closure, tray, strand_a, strand_b, _access_point = _plant(db_session)
    db_session.commit()

    result = field_fiber.propose_splice(
        db_session,
        _auth(user),
        closure_id=str(closure.id),
        from_strand_id=str(strand_a.id),
        to_strand_id=str(strand_b.id),
        tray_id=str(tray.id),
        position=1,
        splice_type="fusion",
        loss_db=0.12,
        note="Field captured splice",
    )

    assert result["status"] == "pending"
    assert result["replayed"] is False
    change = db_session.query(FiberChangeRequest).one()
    assert change.asset_type == "fiber_splice"
    assert change.payload["field_actor"]["system_user_id"] == str(user.id)
    assert change.payload["loss_db"] == 0.12

    replay = field_fiber.propose_splice(
        db_session,
        _auth(user),
        closure_id=str(closure.id),
        from_strand_id=str(strand_b.id),
        to_strand_id=str(strand_a.id),
    )
    assert replay["change_request_id"] == change.id
    assert replay["replayed"] is True


def test_propose_splice_rejects_in_use_strand(db_session):
    user = _user(db_session)
    _profile(db_session, user)
    closure, _tray, strand_a, strand_b, _access_point = _plant(db_session)
    strand_b.status = FiberStrandStatus.in_use
    db_session.commit()

    with pytest.raises(HTTPException) as exc:
        field_fiber.propose_splice(
            db_session,
            _auth(user),
            closure_id=str(closure.id),
            from_strand_id=str(strand_a.id),
            to_strand_id=str(strand_b.id),
        )

    assert exc.value.status_code == 422


def test_record_fiber_test_is_scoped_and_idempotent(db_session):
    user = _user(db_session)
    _profile(db_session, user)
    subscriber = _subscriber(db_session)
    _work_order(db_session, subscriber)
    _closure, _tray, _strand_a, _strand_b, access_point = _plant(db_session)
    client_ref = uuid4()
    db_session.commit()

    result = field_fiber.record_test(
        db_session,
        _auth(user),
        crm_work_order_id="wo-fiber",
        asset_type="fiber_access_point",
        asset_id=str(access_point.id),
        test_type="optical_power",
        wavelength_nm=1490,
        value_db=-20.5,
        unit="dBm",
        passed=True,
        instrument="Power meter",
        client_ref=str(client_ref),
    )

    replay = field_fiber.record_test(
        db_session,
        _auth(user),
        crm_work_order_id="wo-fiber",
        asset_type="fiber_access_point",
        asset_id=str(access_point.id),
        test_type="optical_power",
        client_ref=str(client_ref),
    )
    assert replay.id == result.id
    assert db_session.query(FieldFiberTestResult).count() == 1

    rows = field_fiber.list_tests(db_session, _auth(user), crm_work_order_id="wo-fiber")
    assert [row.id for row in rows] == [result.id]
    assert rows[0].value_db == -20.5
