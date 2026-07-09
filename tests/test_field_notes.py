from __future__ import annotations

from datetime import UTC, datetime
from uuid import uuid4

import pytest
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient

from app.api.field import router
from app.db import get_db
from app.models.dispatch import TechnicianProfile
from app.models.field_note import FieldWorkOrderNote
from app.models.subscriber import Subscriber, UserType
from app.models.system_user import SystemUser
from app.models.work_order_mirror import WorkOrderMirror
from app.services.auth_dependencies import require_user_auth
from app.services.field.jobs import field_jobs
from app.services.field.notes import field_notes


def _user(db_session, name: str = "Note") -> SystemUser:
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
    db_session, user: SystemUser, crm_person_id: str = "crm-note-tech"
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
        first_name="Note",
        last_name="Customer",
        email=f"note-{uuid4().hex[:8]}@example.com",
    )
    db_session.add(subscriber)
    db_session.flush()
    return subscriber


def _work_order(db_session, subscriber: Subscriber, **overrides) -> WorkOrderMirror:
    row = WorkOrderMirror(
        crm_work_order_id=overrides.pop("crm_work_order_id", "wo-note"),
        subscriber_id=subscriber.id,
        title=overrides.pop("title", "Field job"),
        status=overrides.pop("status", "dispatched"),
        assigned_to_crm_person_id=overrides.pop(
            "assigned_to_crm_person_id", "crm-note-tech"
        ),
        address=overrides.pop("address", "Jabi"),
        scheduled_start=overrides.pop("scheduled_start", datetime.now(UTC)),
        **overrides,
    )
    db_session.add(row)
    db_session.flush()
    return row


def test_create_field_note_and_surface_in_job_detail(db_session):
    user = _user(db_session)
    _profile(db_session, user)
    subscriber = _subscriber(db_session)
    work_order = _work_order(db_session, subscriber, crm_work_order_id="wo-note-detail")
    db_session.commit()

    note = field_notes.create(
        db_session,
        _auth(user),
        "wo-note-detail",
        body="  Confirmed access with customer.  ",
        is_internal=False,
    )

    assert note["body"] == "Confirmed access with customer."
    assert note["author_name"] == "Note Tech"
    assert note["is_internal"] is False
    stored = db_session.query(FieldWorkOrderNote).one()
    assert stored.work_order_mirror_id == work_order.id
    assert stored.crm_work_order_id == "wo-note-detail"

    detail = field_jobs.get_detail(db_session, _auth(user), "wo-note-detail")
    assert len(detail.notes) == 1
    assert detail.notes[0].body == "Confirmed access with customer."


def test_field_note_does_not_leak_unassigned_jobs(db_session):
    user = _user(db_session)
    _profile(db_session, user)
    other_user = _user(db_session, "Other")
    _profile(db_session, other_user, crm_person_id="other-tech")
    subscriber = _subscriber(db_session)
    _work_order(
        db_session,
        subscriber,
        crm_work_order_id="wo-hidden-note",
        assigned_to_crm_person_id="other-tech",
    )
    db_session.commit()

    with pytest.raises(HTTPException) as exc:
        field_notes.create(
            db_session,
            _auth(user),
            "wo-hidden-note",
            body="Should not work",
        )

    assert exc.value.status_code == 404


def test_note_attachments_rejected_until_attachment_foundation_lands(db_session):
    user = _user(db_session)
    _profile(db_session, user)
    subscriber = _subscriber(db_session)
    _work_order(db_session, subscriber, crm_work_order_id="wo-note-attachments")
    db_session.commit()

    with pytest.raises(HTTPException) as exc:
        field_notes.create(
            db_session,
            _auth(user),
            "wo-note-attachments",
            body="Photo attached",
            attachment_ids=[str(uuid4())],
        )

    assert exc.value.status_code == 422


def test_field_note_api(db_session):
    user = _user(db_session)
    _profile(db_session, user)
    subscriber = _subscriber(db_session)
    _work_order(db_session, subscriber, crm_work_order_id="wo-note-api")
    db_session.commit()

    app = FastAPI()
    app.include_router(router, prefix="/api/v1")
    app.dependency_overrides[get_db] = lambda: db_session
    app.dependency_overrides[require_user_auth] = lambda: _auth(user)

    resp = TestClient(app).post(
        "/api/v1/field/jobs/wo-note-api/notes",
        json={"body": "Customer asked for a morning visit", "is_internal": False},
    )

    assert resp.status_code == 201
    assert resp.json()["body"] == "Customer asked for a morning visit"
    assert resp.json()["author_name"] == "Note Tech"
