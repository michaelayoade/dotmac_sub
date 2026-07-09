from __future__ import annotations

from collections.abc import Iterator
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient

from app.api.field import router
from app.db import get_db
from app.models.dispatch import TechnicianProfile
from app.models.field_job_event import FieldJobEvent
from app.models.field_worklog import FieldWorkLog
from app.models.stored_file import StoredFile
from app.models.subscriber import Subscriber, UserType
from app.models.system_user import SystemUser
from app.models.work_order_mirror import WorkOrderMirror
from app.services.auth_dependencies import require_user_auth
from app.services.field import attachments as attachments_module
from app.services.field.attachments import field_attachments
from app.services.field.jobs import field_jobs
from app.services.field.transitions import field_transitions


def _with_utc(value: datetime) -> datetime:
    return value if value.tzinfo else value.replace(tzinfo=UTC)


@dataclass
class _Stream:
    chunks: Iterator[bytes]
    content_type: str
    content_length: int


class _FakeUploads:
    def __init__(self):
        self.contents: dict[str, bytes] = {}

    def upload(self, **kwargs):
        record = StoredFile(
            entity_type=kwargs["entity_type"],
            entity_id=kwargs["entity_id"],
            original_filename=kwargs["original_filename"],
            storage_key_or_relative_path=f"attachments/{uuid4().hex}",
            file_size=len(kwargs["data"]),
            content_type=kwargs["content_type"],
            storage_provider="s3",
            uploaded_by=kwargs["uploaded_by"],
            owner_subscriber_id=kwargs["owner_subscriber_id"],
        )
        kwargs["db"].add(record)
        kwargs["db"].commit()
        kwargs["db"].refresh(record)
        self.contents[str(record.id)] = kwargs["data"]
        return record

    def stream_file(self, record):
        data = self.contents[str(record.id)]
        return _Stream(iter([data]), record.content_type, len(data))

    def soft_delete(self, *, db, file, hard_delete_object=True):
        file.is_deleted = True
        db.commit()
        return file


@pytest.fixture()
def fake_uploads(monkeypatch):
    fake = _FakeUploads()
    monkeypatch.setattr(attachments_module, "file_uploads", fake)
    return fake


def _user(db_session, name: str = "Transition") -> SystemUser:
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
    db_session, user: SystemUser, crm_person_id: str = "crm-transition-tech"
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
        first_name="Transition",
        last_name="Customer",
        email=f"transition-{uuid4().hex[:8]}@example.com",
    )
    db_session.add(subscriber)
    db_session.flush()
    return subscriber


def _work_order(db_session, subscriber: Subscriber, **overrides) -> WorkOrderMirror:
    row = WorkOrderMirror(
        crm_work_order_id=overrides.pop("crm_work_order_id", "wo-transition"),
        subscriber_id=subscriber.id,
        title=overrides.pop("title", "Fibre install"),
        status=overrides.pop("status", "dispatched"),
        assigned_to_crm_person_id=overrides.pop(
            "assigned_to_crm_person_id", "crm-transition-tech"
        ),
        scheduled_start=overrides.pop("scheduled_start", datetime.now(UTC)),
        **overrides,
    )
    db_session.add(row)
    db_session.flush()
    return row


def _attach_photo(db_session, user, crm_work_order_id: str, *, kind: str = "photo"):
    return field_attachments.create(
        db_session,
        _auth(user),
        kind=kind,
        file_name=f"{kind}.jpg",
        mime_type="image/jpeg",
        content=b"image-bytes",
        crm_work_order_id=crm_work_order_id,
    )


def test_transition_start_replay_and_pause_updates_mirror_timer_and_history(db_session):
    user = _user(db_session)
    _profile(db_session, user)
    subscriber = _subscriber(db_session)
    _work_order(db_session, subscriber, crm_work_order_id="wo-transition-flow")
    started = datetime.now(UTC) - timedelta(minutes=30)
    db_session.commit()

    start_ref = uuid4()
    started_result = field_transitions.apply(
        db_session,
        _auth(user),
        "wo-transition-flow",
        event="start",
        client_event_id=start_ref,
        occurred_at=started,
    )
    replayed = field_transitions.apply(
        db_session,
        _auth(user),
        "wo-transition-flow",
        event="start",
        client_event_id=start_ref,
        occurred_at=started,
    )

    assert started_result["job"].status == "in_progress"
    assert replayed["replayed"] is True
    assert db_session.query(FieldJobEvent).count() == 1
    open_log = db_session.query(FieldWorkLog).one()
    assert open_log.end_at is None

    paused_at = started + timedelta(minutes=30)
    paused = field_transitions.apply(
        db_session,
        _auth(user),
        "wo-transition-flow",
        event="pause",
        client_event_id=uuid4(),
        occurred_at=paused_at,
        note="Waiting for access",
    )

    assert paused["job"].status == "paused"
    assert paused["event"]["note"] == "Waiting for access"
    db_session.refresh(open_log)
    assert _with_utc(open_log.end_at) == paused_at
    assert open_log.minutes == 30

    detail = field_jobs.get_detail(db_session, _auth(user), "wo-transition-flow")
    assert [event.event for event in detail.events] == ["start", "pause"]


def test_completion_requires_photo_and_signature_fallback(db_session, fake_uploads):
    user = _user(db_session)
    _profile(db_session, user)
    subscriber = _subscriber(db_session)
    _work_order(
        db_session,
        subscriber,
        crm_work_order_id="wo-transition-complete",
        status="in_progress",
    )
    db_session.commit()

    with pytest.raises(HTTPException) as exc:
        field_transitions.apply(
            db_session,
            _auth(user),
            "wo-transition-complete",
            event="complete",
            client_event_id=uuid4(),
        )

    assert exc.value.status_code == 422
    assert exc.value.detail == "Completion requires at least one photo"

    _attach_photo(db_session, user, "wo-transition-complete")
    completed_at = datetime.now(UTC)
    completed = field_transitions.apply(
        db_session,
        _auth(user),
        "wo-transition-complete",
        event="complete",
        client_event_id=uuid4(),
        occurred_at=completed_at,
        payload={"signature_unavailable_reason": "Customer unavailable"},
    )

    assert completed["job"].status == "completed"
    assert _with_utc(completed["job"].completed_at) == completed_at
    assert completed["job"].metadata_["native_transition_pending_sync"] is True
    assert completed["event"]["new_status"] == "completed"


def test_transition_rejects_hidden_jobs_and_invalid_unable_reason(db_session):
    user = _user(db_session)
    _profile(db_session, user)
    other = _user(db_session, "Other")
    _profile(db_session, other, crm_person_id="other-transition-tech")
    subscriber = _subscriber(db_session)
    _work_order(
        db_session,
        subscriber,
        crm_work_order_id="wo-transition-hidden",
        assigned_to_crm_person_id="other-transition-tech",
    )
    _work_order(db_session, subscriber, crm_work_order_id="wo-transition-unable")
    db_session.commit()

    with pytest.raises(HTTPException) as hidden:
        field_transitions.apply(
            db_session,
            _auth(user),
            "wo-transition-hidden",
            event="start",
            client_event_id=uuid4(),
        )
    assert hidden.value.status_code == 404

    with pytest.raises(HTTPException) as invalid:
        field_transitions.apply(
            db_session,
            _auth(user),
            "wo-transition-unable",
            event="unable_to_complete",
            client_event_id=uuid4(),
            payload={"reason": "bad_reason"},
        )
    assert invalid.value.status_code == 422


def test_transition_api(db_session):
    user = _user(db_session)
    _profile(db_session, user)
    subscriber = _subscriber(db_session)
    _work_order(db_session, subscriber, crm_work_order_id="wo-transition-api")
    db_session.commit()

    app = FastAPI()
    app.include_router(router, prefix="/api/v1")
    app.dependency_overrides[get_db] = lambda: db_session
    app.dependency_overrides[require_user_auth] = lambda: _auth(user)

    resp = TestClient(app).post(
        "/api/v1/field/jobs/wo-transition-api/transition",
        json={"event": "start", "client_event_id": str(uuid4())},
    )

    assert resp.status_code == 201
    body = resp.json()
    assert body["job"]["status"] == "in_progress"
    assert body["event"]["event"] == "start"
    assert body["replayed"] is False
