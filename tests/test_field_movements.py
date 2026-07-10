from __future__ import annotations

from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
from fastapi import HTTPException

from app.models.dispatch import TechnicianProfile
from app.models.field_movement import FieldWorkOrderMovement
from app.models.subscriber import Subscriber, UserType
from app.models.system_user import SystemUser
from app.models.work_order_mirror import WorkOrderMirror
from app.services.field.jobs import field_jobs
from app.services.field.transitions import field_transitions


def _with_utc(value: datetime) -> datetime:
    return value if value.tzinfo else value.replace(tzinfo=UTC)


def _user(db_session, name: str = "Move") -> SystemUser:
    user = SystemUser(
        first_name=name,
        last_name="Tech",
        display_name=f"{name} Tech",
        email=f"{name.lower()}-{uuid4().hex[:8]}@example.com",
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


def _profile(
    db_session, user: SystemUser, crm_person_id: str = "crm-move-tech"
) -> TechnicianProfile:
    profile = TechnicianProfile(
        person_id=user.id,
        system_user_id=user.id,
        crm_person_id=crm_person_id,
        title="Installer",
    )
    db_session.add(profile)
    db_session.flush()
    return profile


def _subscriber(db_session) -> Subscriber:
    subscriber = Subscriber(
        first_name="Move",
        last_name="Customer",
        email=f"move-{uuid4().hex[:8]}@example.com",
    )
    db_session.add(subscriber)
    db_session.flush()
    return subscriber


def _work_order(db_session, subscriber: Subscriber, **overrides) -> WorkOrderMirror:
    row = WorkOrderMirror(
        crm_work_order_id=overrides.pop("crm_work_order_id", "wo-move"),
        subscriber_id=subscriber.id,
        title=overrides.pop("title", "Travel to site"),
        status=overrides.pop("status", "dispatched"),
        assigned_to_crm_person_id=overrides.pop(
            "assigned_to_crm_person_id", "crm-move-tech"
        ),
        scheduled_start=overrides.pop("scheduled_start", datetime.now(UTC)),
        metadata_=overrides.pop(
            "metadata_", {"location": {"lat": 9.071, "lng": 7.451}}
        ),
        **overrides,
    )
    db_session.add(row)
    db_session.flush()
    return row


def test_en_route_and_arrived_manage_movement_session(db_session):
    user = _user(db_session)
    _profile(db_session, user)
    subscriber = _subscriber(db_session)
    _work_order(db_session, subscriber, crm_work_order_id="wo-move-flow")
    started = datetime.now(UTC) - timedelta(minutes=12)
    arrived_at = datetime.now(UTC)
    db_session.commit()

    field_transitions.apply(
        db_session,
        _auth(user),
        "wo-move-flow",
        event="en_route",
        client_event_id=uuid4(),
        occurred_at=started,
        latitude=9.0,
        longitude=7.4,
        payload={
            "destination_type": "fdh",
            "destination_id": "FDH-12",
            "destination_label": "FDH 12",
            "destination_latitude": 9.0712,
            "destination_longitude": 7.4512,
        },
    )
    movement = db_session.query(FieldWorkOrderMovement).one()
    assert movement.status == "en_route"
    assert movement.destination_type == "cabinet"
    assert movement.destination_label == "FDH 12"
    assert movement.start_latitude == 9.0

    field_transitions.apply(
        db_session,
        _auth(user),
        "wo-move-flow",
        event="arrived",
        client_event_id=uuid4(),
        occurred_at=arrived_at,
        latitude=9.0712,
        longitude=7.4512,
        payload={"movement_session_id": str(movement.id)},
    )

    db_session.refresh(movement)
    assert movement.status == "arrived"
    assert _with_utc(movement.arrived_at) == arrived_at
    assert movement.arrival_latitude == 9.0712

    detail = field_jobs.get_detail(db_session, _auth(user), "wo-move-flow")
    assert len(detail.movements) == 1
    assert detail.movements[0].status == "arrived"


def test_movement_rejects_invalid_destination(db_session):
    user = _user(db_session)
    _profile(db_session, user)
    subscriber = _subscriber(db_session)
    _work_order(db_session, subscriber, crm_work_order_id="wo-move-invalid")
    db_session.commit()

    with pytest.raises(HTTPException) as exc:
        field_transitions.apply(
            db_session,
            _auth(user),
            "wo-move-invalid",
            event="en_route",
            client_event_id=uuid4(),
            payload={"destination_type": "not-real"},
        )

    assert exc.value.status_code == 422
    assert db_session.query(FieldWorkOrderMovement).count() == 0
