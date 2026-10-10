from __future__ import annotations

import hashlib
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from app.api.field import router
from app.db import get_db
from app.models.dispatch import TechnicianProfile
from app.models.domain_settings import DomainSetting, SettingDomain
from app.models.field_job_event import FieldJobEvent
from app.models.field_worklog import FieldWorkLog
from app.models.stored_file import StoredFile
from app.models.subscriber import Subscriber, UserType
from app.models.subscription_engine import SettingValueType
from app.models.support import Ticket, TicketComment
from app.models.system_user import SystemUser
from app.models.work_order import WorkOrder
from app.schemas.field import FieldAttachmentRead, FieldTransitionResponse
from app.services.auth_dependencies import require_user_auth
from app.services.db_session_adapter import db_session_adapter
from app.services.field import attachments as attachments_module
from app.services.field.attachments import field_attachments
from app.services.field.execution_contracts import (
    ApplyFieldTransition,
    CreateFieldAttachment,
    FieldEvent,
    FieldJobQuery,
    FieldTransitionPayload,
)
from app.services.field.jobs import field_jobs
from app.services.field.transitions import field_transitions
from app.services.field.work_order_access import FieldAccessError
from app.services.file_storage import UnifiedFileUploadService
from app.services.owner_commands import CommandContext


def _with_utc(value: datetime) -> datetime:
    return value if value.tzinfo else value.replace(tzinfo=UTC)


@dataclass
class _Stream:
    chunks: Iterator[bytes]
    content_type: str
    content_length: int


class _FakeUploads(UnifiedFileUploadService):
    def __init__(self):
        self.contents: dict[str, bytes] = {}

    def stage_upload(self, **kwargs):
        record = StoredFile(
            entity_type=kwargs["entity_type"],
            entity_id=kwargs["entity_id"],
            original_filename=kwargs["original_filename"],
            storage_key_or_relative_path=f"attachments/{uuid4().hex}",
            file_size=len(kwargs["data"]),
            checksum=hashlib.sha256(kwargs["data"]).hexdigest(),
            content_type=kwargs["content_type"],
            storage_provider="s3",
            uploaded_by=kwargs["uploaded_by"],
            owner_subscriber_id=kwargs["owner_subscriber_id"],
        )
        kwargs["db"].add(record)
        kwargs["db"].flush()
        kwargs["db"].refresh(record)
        self.contents[str(record.id)] = kwargs["data"]
        return record

    def stream_file(self, record):
        data = self.contents[str(record.id)]
        return _Stream(iter([data]), record.content_type, len(data))

    def stage_soft_delete(self, *, db, file, hard_delete_object=True):
        file.is_deleted = True
        db.flush()
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


def _work_order(db_session, subscriber: Subscriber, **overrides) -> WorkOrder:
    row = WorkOrder(
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
    return _attachments_create(
        db=db_session,
        command=CreateFieldAttachment(
            requester_system_user_id=user.id,
            kind=kind,
            file_name=f"{kind}.jpg",
            mime_type="image/jpeg",
            content=b"image-bytes",
            public_id=crm_work_order_id,
            context=CommandContext.system(
                actor=f"user:{user.id}",
                scope="field:test",
                reason="test_field_execution",
                idempotency_key=str(uuid4()),
            ),
        ),
    )


def test_transition_start_replay_and_pause_updates_mirror_timer_and_history(db_session):
    user = _user(db_session)
    _profile(db_session, user)
    subscriber = _subscriber(db_session)
    _work_order(db_session, subscriber, crm_work_order_id="wo-transition-flow")
    started = datetime.now(UTC) - timedelta(minutes=30)
    db_session.commit()

    start_ref = uuid4()
    started_result = _transitions_apply(
        db=db_session,
        command=ApplyFieldTransition(
            requester_system_user_id=user.id,
            public_id="wo-transition-flow",
            event=FieldEvent("start"),
            client_event_id=start_ref,
            occurred_at=started,
            context=CommandContext.system(
                actor=f"user:{user.id}",
                scope="field:test",
                reason="test_field_execution",
                idempotency_key=str(uuid4()),
            ),
        ),
    )
    replayed = _transitions_apply(
        db=db_session,
        command=ApplyFieldTransition(
            requester_system_user_id=user.id,
            public_id="wo-transition-flow",
            event=FieldEvent("start"),
            client_event_id=start_ref,
            occurred_at=started,
            context=CommandContext.system(
                actor=f"user:{user.id}",
                scope="field:test",
                reason="test_field_execution",
                idempotency_key=str(uuid4()),
            ),
        ),
    )

    assert started_result.job.status == "in_progress"
    assert replayed.replayed is True
    assert db_session.query(FieldJobEvent).count() == 1
    open_log = db_session.query(FieldWorkLog).one()
    assert open_log.end_at is None

    paused_at = started + timedelta(minutes=30)
    paused = _transitions_apply(
        db=db_session,
        command=ApplyFieldTransition(
            requester_system_user_id=user.id,
            public_id="wo-transition-flow",
            event=FieldEvent("pause"),
            client_event_id=uuid4(),
            occurred_at=paused_at,
            note="Waiting for access",
            context=CommandContext.system(
                actor=f"user:{user.id}",
                scope="field:test",
                reason="test_field_execution",
                idempotency_key=str(uuid4()),
            ),
        ),
    )

    assert paused.job.status == "paused"
    assert paused.event.note == "Waiting for access"
    db_session.refresh(open_log)
    assert _with_utc(open_log.end_at) == paused_at
    assert open_log.minutes == 30

    detail = field_jobs.get_detail(
        db=db_session,
        query=FieldJobQuery(
            requester_system_user_id=user.id, public_id="wo-transition-flow"
        ),
    )
    assert [event.event for event in detail.events] == ["start", "pause"]


def test_completion_requires_photo_and_signature_fallback(db_session, fake_uploads):
    user = _user(db_session)
    _profile(db_session, user)
    subscriber = _subscriber(db_session)
    ticket = Ticket(
        title="Loss of signal incident",
        subscriber_id=subscriber.id,
        customer_account_id=subscriber.id,
        status="open",
        priority="high",
    )
    db_session.add(ticket)
    db_session.flush()
    _work_order(
        db_session,
        subscriber,
        crm_work_order_id="wo-transition-complete",
        status="in_progress",
        origin_ticket_id=ticket.id,
    )
    db_session.commit()

    requirements = field_transitions.completion_requirements(db_session)
    assert requirements.evidence_required is True
    assert requirements.minimum_photo_count == 1
    assert requirements.customer_signoff_required is True
    assert requirements.signature_unavailable_reason_allowed is True

    with pytest.raises(FieldAccessError) as exc:
        _transitions_apply(
            db=db_session,
            command=ApplyFieldTransition(
                requester_system_user_id=user.id,
                public_id="wo-transition-complete",
                event=FieldEvent("complete"),
                client_event_id=uuid4(),
                context=CommandContext.system(
                    actor=f"user:{user.id}",
                    scope="field:test",
                    reason="test_field_execution",
                    idempotency_key=str(uuid4()),
                ),
            ),
        )

    assert exc.value.code.endswith("invalid_request")
    assert exc.value.message == "Completion requires at least one photo"

    _attach_photo(db_session, user, "wo-transition-complete")
    completed_at = datetime.now(UTC)
    completion_event_id = uuid4()
    completed = _transitions_apply(
        db=db_session,
        command=ApplyFieldTransition(
            requester_system_user_id=user.id,
            public_id="wo-transition-complete",
            event=FieldEvent("complete"),
            client_event_id=completion_event_id,
            occurred_at=completed_at,
            payload=FieldTransitionPayload(
                **{"signature_unavailable_reason": "Customer unavailable"}
            ),
            context=CommandContext.system(
                actor=f"user:{user.id}",
                scope="field:test",
                reason="test_field_execution",
                idempotency_key=str(uuid4()),
            ),
        ),
    )

    assert completed.job.status == "completed"
    assert _with_utc(completed.job.completed_at) == completed_at
    assert (
        db_session.query(WorkOrder)
        .filter_by(public_id="wo-transition-complete")
        .one()
        .metadata_["native_field_source"]
        == "sub"
    )
    assert (
        db_session.query(WorkOrder)
        .filter_by(public_id="wo-transition-complete")
        .one()
        .metadata_["native_field_activity"]["transition"]["event"]
        == "complete"
    )
    assert completed.event.new_status == "completed"
    projection = db_session.query(TicketComment).filter_by(ticket_id=ticket.id).one()
    assert projection.is_internal is True
    assert projection.metadata_["source"] == "work_order_field_outcome"
    assert projection.metadata_["work_order_id"] == "wo-transition-complete"
    assert projection.metadata_["field_event_id"] == str(completed.event.id)
    assert projection.metadata_["outcome"] == "complete"
    assert "Support verification is required" in projection.body
    db_session.refresh(ticket)
    assert ticket.status == "open"

    replayed = _transitions_apply(
        db=db_session,
        command=ApplyFieldTransition(
            requester_system_user_id=user.id,
            public_id="wo-transition-complete",
            event=FieldEvent("complete"),
            client_event_id=completion_event_id,
            occurred_at=completed_at,
            payload=FieldTransitionPayload(
                **{"signature_unavailable_reason": "Customer unavailable"}
            ),
            context=CommandContext.system(
                actor=f"user:{user.id}",
                scope="field:test",
                reason="test_field_execution",
                idempotency_key=str(uuid4()),
            ),
        ),
    )
    assert replayed.replayed is True
    assert db_session.query(TicketComment).filter_by(ticket_id=ticket.id).count() == 1


def test_disabled_completion_evidence_policy_allows_completion_without_evidence(
    db_session,
):
    user = _user(db_session, "NoEvidence")
    _profile(db_session, user)
    subscriber = _subscriber(db_session)
    _work_order(
        db_session,
        subscriber,
        crm_work_order_id="wo-transition-no-evidence",
        status="in_progress",
    )
    db_session.add(
        DomainSetting(
            domain=SettingDomain.field,
            key="completion_requires_evidence",
            value_type=SettingValueType.boolean,
            value_text="false",
        )
    )
    db_session.commit()

    requirements = field_transitions.completion_requirements(db_session)
    completed = _transitions_apply(
        db=db_session,
        command=ApplyFieldTransition(
            requester_system_user_id=user.id,
            public_id="wo-transition-no-evidence",
            event=FieldEvent("complete"),
            client_event_id=uuid4(),
            context=CommandContext.system(
                actor=f"user:{user.id}",
                scope="field:test",
                reason="test_field_execution",
                idempotency_key=str(uuid4()),
            ),
        ),
    )

    assert requirements.evidence_required is False
    assert requirements.minimum_photo_count == 0
    assert requirements.customer_signoff_required is False
    assert requirements.signature_unavailable_reason_allowed is False
    assert completed.job.status == "completed"


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

    with pytest.raises(FieldAccessError) as hidden:
        _transitions_apply(
            db=db_session,
            command=ApplyFieldTransition(
                requester_system_user_id=user.id,
                public_id="wo-transition-hidden",
                event=FieldEvent("start"),
                client_event_id=uuid4(),
                context=CommandContext.system(
                    actor=f"user:{user.id}",
                    scope="field:test",
                    reason="test_field_execution",
                    idempotency_key=str(uuid4()),
                ),
            ),
        )
    assert hidden.value.code.endswith("not_found")

    with pytest.raises(FieldAccessError) as invalid:
        _transitions_apply(
            db=db_session,
            command=ApplyFieldTransition(
                requester_system_user_id=user.id,
                public_id="wo-transition-unable",
                event=FieldEvent("unable_to_complete"),
                client_event_id=uuid4(),
                payload=FieldTransitionPayload(**{"reason": "bad_reason"}),
                context=CommandContext.system(
                    actor=f"user:{user.id}",
                    scope="field:test",
                    reason="test_field_execution",
                    idempotency_key=str(uuid4()),
                ),
            ),
        )
    assert invalid.value.code.endswith("invalid_request")


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


def test_project_work_order_completion_requires_fiber_as_built_evidence(
    db_session, fake_uploads
):
    from app.models.field_fiber import FieldFiberTestResult
    from app.models.project import Project
    from app.services.field.transitions import resolve_fiber_as_built_evidence

    user = _user(db_session, "FiberAsBuilt")
    profile = _profile(db_session, user, crm_person_id="crm-fiber-asbuilt")
    subscriber = _subscriber(db_session)
    project = Project(name="OSP build phase 2")
    db_session.add(project)
    db_session.flush()
    row = _work_order(
        db_session,
        subscriber,
        crm_work_order_id="wo-fiber-asbuilt",
        status="in_progress",
        assigned_to_crm_person_id="crm-fiber-asbuilt",
        project_id=project.id,
    )
    db_session.commit()

    _attach_photo(db_session, user, "wo-fiber-asbuilt")

    evidence = resolve_fiber_as_built_evidence(db_session, row)
    assert evidence.required is True
    assert evidence.satisfied is False

    with pytest.raises(FieldAccessError) as exc:
        _transitions_apply(
            db=db_session,
            command=ApplyFieldTransition(
                requester_system_user_id=user.id,
                public_id="wo-fiber-asbuilt",
                event=FieldEvent("complete"),
                client_event_id=uuid4(),
                payload=FieldTransitionPayload(
                    **{"signature_unavailable_reason": "Plant work, no customer"}
                ),
                context=CommandContext.system(
                    actor=f"user:{user.id}",
                    scope="field:test",
                    reason="test_field_execution",
                    idempotency_key=str(uuid4()),
                ),
            ),
        )

    assert exc.value.code.endswith("invalid_request")
    assert "fiber" in exc.value.message

    db_session.add(
        FieldFiberTestResult(
            work_order_mirror_id=row.id,
            asset_type="fiber_access_point",
            asset_id=uuid4(),
            test_type="optical_power",
            value_db=-21.3,
            unit="dBm",
            passed=True,
            measured_by_technician_id=profile.id,
            measured_by_person_id=user.id,
        )
    )
    db_session.commit()

    evidence = resolve_fiber_as_built_evidence(db_session, row)
    assert evidence.satisfied is True
    assert evidence.fiber_test_count == 1

    completed = _transitions_apply(
        db=db_session,
        command=ApplyFieldTransition(
            requester_system_user_id=user.id,
            public_id="wo-fiber-asbuilt",
            event=FieldEvent("complete"),
            client_event_id=uuid4(),
            payload=FieldTransitionPayload(
                **{"signature_unavailable_reason": "Plant work, no customer"}
            ),
            context=CommandContext.system(
                actor=f"user:{user.id}",
                scope="field:test",
                reason="test_field_execution",
                idempotency_key=str(uuid4()),
            ),
        ),
    )
    assert completed.job.status == "completed"


def test_non_project_work_order_completion_skips_fiber_as_built_gate(
    db_session, fake_uploads
):
    from app.services.field.transitions import resolve_fiber_as_built_evidence

    user = _user(db_session, "NoProject")
    _profile(db_session, user, crm_person_id="crm-no-project")
    subscriber = _subscriber(db_session)
    row = _work_order(
        db_session,
        subscriber,
        crm_work_order_id="wo-no-project",
        status="in_progress",
        assigned_to_crm_person_id="crm-no-project",
    )
    db_session.commit()

    evidence = resolve_fiber_as_built_evidence(db_session, row)
    assert evidence.required is False
    assert evidence.satisfied is True

    _attach_photo(db_session, user, "wo-no-project")
    completed = _transitions_apply(
        db=db_session,
        command=ApplyFieldTransition(
            requester_system_user_id=user.id,
            public_id="wo-no-project",
            event=FieldEvent("complete"),
            client_event_id=uuid4(),
            payload=FieldTransitionPayload(
                **{"signature_unavailable_reason": "Customer unavailable"}
            ),
            context=CommandContext.system(
                actor=f"user:{user.id}",
                scope="field:test",
                reason="test_field_execution",
                idempotency_key=str(uuid4()),
            ),
        ),
    )
    assert completed.job.status == "completed"


def _attachments_create(
    db: Session, command: CreateFieldAttachment
) -> FieldAttachmentRead:
    db_session_adapter.release_read_transaction(db)
    return field_attachments.create(db=db, command=command)


def _transitions_apply(
    db: Session, command: ApplyFieldTransition
) -> FieldTransitionResponse:
    db_session_adapter.release_read_transaction(db)
    return field_transitions.apply(db=db, command=command)
