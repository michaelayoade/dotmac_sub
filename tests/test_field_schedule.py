from __future__ import annotations

from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient

from app.api.field import router
from app.db import get_db
from app.models.dispatch import AvailabilityBlock, Shift, TechnicianProfile
from app.models.subscriber import Subscriber, UserType
from app.models.system_user import SystemUser
from app.models.work_order_mirror import WorkOrderMirror
from app.services.auth_dependencies import require_user_auth
from app.services.field.schedule import field_schedule


def _user(db_session, name: str = "Ade") -> SystemUser:
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
    db_session, user: SystemUser, crm_person_id: str = "crm-tech-1"
) -> TechnicianProfile:
    profile = TechnicianProfile(
        person_id=user.id,
        system_user_id=user.id,
        crm_person_id=crm_person_id,
        title="Installer",
        region="Jabi",
    )
    db_session.add(profile)
    db_session.flush()
    return profile


def _subscriber(db_session) -> Subscriber:
    sub = Subscriber(
        first_name="Adaeze",
        last_name="Nwosu",
        email=f"adaeze-{uuid4().hex[:8]}@example.com",
    )
    db_session.add(sub)
    db_session.flush()
    return sub


def _work_order(db_session, subscriber: Subscriber, **overrides) -> WorkOrderMirror:
    row = WorkOrderMirror(
        crm_work_order_id=overrides.pop("crm_work_order_id", f"wo-{uuid4().hex[:8]}"),
        subscriber_id=subscriber.id,
        title=overrides.pop("title", "Fibre install"),
        status=overrides.pop("status", "scheduled"),
        assigned_to_crm_person_id=overrides.pop(
            "assigned_to_crm_person_id", "crm-tech-1"
        ),
        scheduled_start=overrides.pop("scheduled_start"),
        scheduled_end=overrides.pop("scheduled_end", None),
        **overrides,
    )
    db_session.add(row)
    db_session.flush()
    return row


def test_timeline_merges_dispatch_and_mirror_jobs(db_session):
    now = datetime.now(UTC)
    user = _user(db_session)
    profile = _profile(db_session, user)
    subscriber = _subscriber(db_session)
    _work_order(
        db_session,
        subscriber,
        crm_work_order_id="wo-schedule",
        scheduled_start=now + timedelta(hours=4),
        scheduled_end=now + timedelta(hours=6),
    )
    db_session.add(
        Shift(
            technician_id=profile.id,
            start_at=now + timedelta(hours=1),
            end_at=now + timedelta(hours=9),
        )
    )
    db_session.add(
        AvailabilityBlock(
            technician_id=profile.id,
            start_at=now + timedelta(hours=6),
            end_at=now + timedelta(hours=7),
            reason="Training",
        )
    )
    db_session.commit()

    timeline = field_schedule.timeline(db_session, _auth(user), date_from=now)

    assert [entry["type"] for entry in timeline] == ["shift", "job", "availability"]
    assert timeline[1]["reference_id"] == "wo-schedule"
    assert timeline[2]["title"] == "Training"


def test_window_clamped_to_31_days(db_session):
    now = datetime.now(UTC)
    user = _user(db_session)
    profile = _profile(db_session, user)
    db_session.add(
        Shift(
            technician_id=profile.id,
            start_at=now + timedelta(days=40),
            end_at=now + timedelta(days=40, hours=8),
        )
    )
    db_session.commit()

    timeline = field_schedule.timeline(
        db_session,
        _auth(user),
        date_from=now,
        date_to=now + timedelta(days=90),
    )

    assert timeline == []


def test_invalid_window_rejected(db_session):
    now = datetime.now(UTC)
    user = _user(db_session)
    _profile(db_session, user)

    with pytest.raises(HTTPException) as exc:
        field_schedule.timeline(
            db_session,
            _auth(user),
            date_from=now,
            date_to=now - timedelta(days=1),
        )

    assert exc.value.status_code == 422


def test_other_technicians_jobs_not_visible(db_session):
    now = datetime.now(UTC)
    user = _user(db_session)
    _profile(db_session, user, crm_person_id="crm-tech-1")
    subscriber = _subscriber(db_session)
    _work_order(
        db_session,
        subscriber,
        crm_work_order_id="wo-hidden",
        assigned_to_crm_person_id="crm-tech-2",
        scheduled_start=now + timedelta(hours=2),
    )
    db_session.commit()

    assert field_schedule.timeline(db_session, _auth(user), date_from=now) == []


def test_schedule_api_returns_entries(db_session):
    now = datetime.now(UTC)
    user = _user(db_session)
    _profile(db_session, user)
    subscriber = _subscriber(db_session)
    _work_order(
        db_session,
        subscriber,
        crm_work_order_id="wo-api-schedule",
        scheduled_start=now + timedelta(hours=2),
    )
    db_session.commit()

    app = FastAPI()
    app.include_router(router, prefix="/api/v1")
    app.dependency_overrides[get_db] = lambda: db_session
    app.dependency_overrides[require_user_auth] = lambda: _auth(user)

    resp = TestClient(app).get("/api/v1/field/schedule")

    assert resp.status_code == 200
    assert resp.json()[0]["reference_id"] == "wo-api-schedule"
