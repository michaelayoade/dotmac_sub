from __future__ import annotations

from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from app.api.field import router
from app.db import get_db
from app.models.dispatch import TechnicianProfile
from app.models.field_worklog import FieldWorkLog
from app.models.subscriber import Subscriber, UserType
from app.models.system_user import SystemUser
from app.models.work_order import WorkOrder
from app.schemas.field import FieldWorkLogResult
from app.services.auth_dependencies import require_user_auth
from app.services.db_session_adapter import db_session_adapter
from app.services.field.execution_contracts import (
    FieldJobQuery,
    FieldWorkLogEntry,
    SubmitFieldWorkLogs,
)
from app.services.field.jobs import field_jobs
from app.services.field.work_order_access import FieldAccessError
from app.services.field.worklogs import field_worklogs
from app.services.owner_commands import CommandContext


def _user(db_session, name: str = "Log") -> SystemUser:
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
    db_session, user: SystemUser, crm_person_id: str = "crm-log-tech"
) -> TechnicianProfile:
    profile = TechnicianProfile(
        person_id=user.id,
        system_user_id=user.id,
        crm_person_id=crm_person_id,
    )
    db_session.add(profile)
    db_session.flush()
    return profile


def _subscriber(db_session) -> Subscriber:
    subscriber = Subscriber(
        first_name="Log",
        last_name="Customer",
        email=f"log-{uuid4().hex[:8]}@example.com",
    )
    db_session.add(subscriber)
    db_session.flush()
    return subscriber


def _work_order(db_session, subscriber: Subscriber, **overrides) -> WorkOrder:
    row = WorkOrder(
        crm_work_order_id=overrides.pop("crm_work_order_id", "wo-log"),
        subscriber_id=subscriber.id,
        title=overrides.pop("title", "Field job"),
        status=overrides.pop("status", "dispatched"),
        assigned_to_crm_person_id=overrides.pop(
            "assigned_to_crm_person_id", "crm-log-tech"
        ),
        scheduled_start=overrides.pop("scheduled_start", datetime.now(UTC)),
        **overrides,
    )
    db_session.add(row)
    db_session.flush()
    return row


def test_submit_worklog_and_surface_in_job_detail(db_session):
    user = _user(db_session)
    _profile(db_session, user)
    subscriber = _subscriber(db_session)
    _work_order(db_session, subscriber, crm_work_order_id="wo-log-detail")
    start = datetime.now(UTC) - timedelta(hours=2)
    end = start + timedelta(minutes=90)
    db_session.commit()

    result = _worklogs_submit(
        db=db_session,
        command=SubmitFieldWorkLogs(
            requester_system_user_id=user.id,
            public_id="wo-log-detail",
            entries=tuple(
                FieldWorkLogEntry(**entry)
                for entry in [
                    {"start_at": start, "end_at": end, "notes": "Spliced drop"}
                ]
            ),
            context=CommandContext.system(
                actor=f"user:{user.id}",
                scope="field:test",
                reason="test_field_execution",
                idempotency_key=str(uuid4()),
            ),
        ),
    )

    assert result[0].duplicate is False
    assert result[0].worklog.minutes == 90
    stored = db_session.query(FieldWorkLog).one()
    assert stored.work_order_mirror.public_id == "wo-log-detail"

    detail = field_jobs.get_detail(
        db=db_session,
        query=FieldJobQuery(
            requester_system_user_id=user.id, public_id="wo-log-detail"
        ),
    )
    assert len(detail.worklogs) == 1
    assert detail.worklogs[0].minutes == 90


def test_worklog_client_ref_dedupes_retry(db_session):
    user = _user(db_session)
    _profile(db_session, user)
    subscriber = _subscriber(db_session)
    _work_order(db_session, subscriber, crm_work_order_id="wo-log-dedupe")
    start = datetime.now(UTC) - timedelta(hours=1)
    end = start + timedelta(minutes=30)
    client_ref = uuid4()
    db_session.commit()

    first = _worklogs_submit(
        db=db_session,
        command=SubmitFieldWorkLogs(
            requester_system_user_id=user.id,
            public_id="wo-log-dedupe",
            entries=tuple(
                FieldWorkLogEntry(**entry)
                for entry in [
                    {"start_at": start, "end_at": end, "client_ref": client_ref}
                ]
            ),
            context=CommandContext.system(
                actor=f"user:{user.id}",
                scope="field:test",
                reason="test_field_execution",
                idempotency_key=str(uuid4()),
            ),
        ),
    )
    second = _worklogs_submit(
        db=db_session,
        command=SubmitFieldWorkLogs(
            requester_system_user_id=user.id,
            public_id="wo-log-dedupe",
            entries=tuple(
                FieldWorkLogEntry(**entry)
                for entry in [
                    {"start_at": start, "end_at": end, "client_ref": client_ref}
                ]
            ),
            context=CommandContext.system(
                actor=f"user:{user.id}",
                scope="field:test",
                reason="test_field_execution",
                idempotency_key=str(uuid4()),
            ),
        ),
    )

    assert first[0].worklog.id == second[0].worklog.id
    assert second[0].duplicate is True
    assert db_session.query(FieldWorkLog).count() == 1


def test_worklog_overlap_rejected(db_session):
    user = _user(db_session)
    _profile(db_session, user)
    subscriber = _subscriber(db_session)
    _work_order(db_session, subscriber, crm_work_order_id="wo-log-overlap")
    start = datetime.now(UTC) - timedelta(hours=2)
    db_session.commit()

    _worklogs_submit(
        db=db_session,
        command=SubmitFieldWorkLogs(
            requester_system_user_id=user.id,
            public_id="wo-log-overlap",
            entries=tuple(
                FieldWorkLogEntry(**entry)
                for entry in [{"start_at": start, "end_at": start + timedelta(hours=1)}]
            ),
            context=CommandContext.system(
                actor=f"user:{user.id}",
                scope="field:test",
                reason="test_field_execution",
                idempotency_key=str(uuid4()),
            ),
        ),
    )
    with pytest.raises(FieldAccessError) as exc:
        _worklogs_submit(
            db=db_session,
            command=SubmitFieldWorkLogs(
                requester_system_user_id=user.id,
                public_id="wo-log-overlap",
                entries=tuple(
                    FieldWorkLogEntry(**entry)
                    for entry in [
                        {
                            "start_at": start + timedelta(minutes=30),
                            "end_at": start + timedelta(minutes=90),
                        }
                    ]
                ),
                context=CommandContext.system(
                    actor=f"user:{user.id}",
                    scope="field:test",
                    reason="test_field_execution",
                    idempotency_key=str(uuid4()),
                ),
            ),
        )

    assert exc.value.code.endswith("conflict")


def test_worklog_hidden_job_404(db_session):
    user = _user(db_session)
    _profile(db_session, user)
    other = _user(db_session, "Other")
    _profile(db_session, other, crm_person_id="other-log-tech")
    subscriber = _subscriber(db_session)
    _work_order(
        db_session,
        subscriber,
        crm_work_order_id="wo-log-hidden",
        assigned_to_crm_person_id="other-log-tech",
    )
    db_session.commit()

    with pytest.raises(FieldAccessError) as exc:
        _worklogs_submit(
            db=db_session,
            command=SubmitFieldWorkLogs(
                requester_system_user_id=user.id,
                public_id="wo-log-hidden",
                entries=tuple(
                    FieldWorkLogEntry(**entry)
                    for entry in [
                        {
                            "start_at": datetime.now(UTC),
                            "end_at": datetime.now(UTC) + timedelta(minutes=15),
                        }
                    ]
                ),
                context=CommandContext.system(
                    actor=f"user:{user.id}",
                    scope="field:test",
                    reason="test_field_execution",
                    idempotency_key=str(uuid4()),
                ),
            ),
        )

    assert exc.value.code.endswith("not_found")


def test_worklog_api(db_session):
    user = _user(db_session)
    _profile(db_session, user)
    subscriber = _subscriber(db_session)
    _work_order(db_session, subscriber, crm_work_order_id="wo-log-api")
    start = datetime.now(UTC) - timedelta(hours=1)
    end = start + timedelta(minutes=20)
    db_session.commit()

    app = FastAPI()
    app.include_router(router, prefix="/api/v1")
    app.dependency_overrides[get_db] = lambda: db_session
    app.dependency_overrides[require_user_auth] = lambda: _auth(user)

    resp = TestClient(app).post(
        "/api/v1/field/jobs/wo-log-api/worklogs",
        json={
            "entries": [
                {
                    "start_at": start.isoformat(),
                    "end_at": end.isoformat(),
                    "notes": "Completed splice",
                }
            ]
        },
    )

    assert resp.status_code == 200
    assert resp.json()["results"][0]["worklog"]["minutes"] == 20


def _worklogs_submit(
    db: Session, command: SubmitFieldWorkLogs
) -> tuple[FieldWorkLogResult, ...]:
    db_session_adapter.release_read_transaction(db)
    return field_worklogs.submit(db=db, command=command)
