from __future__ import annotations

from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient

from app.api.field import router
from app.db import get_db
from app.models.dispatch import TechnicianProfile
from app.models.field_location import FieldTechLocationPing
from app.models.subscriber import UserType
from app.models.system_user import SystemUser
from app.services.auth_dependencies import require_user_auth
from app.services.field.location_tracking import field_location_tracking


def _user(db_session) -> SystemUser:
    user = SystemUser(
        first_name="Live",
        last_name="Tech",
        display_name="Live Tech",
        email=f"live-{uuid4().hex[:8]}@example.com",
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
        crm_person_id="crm-live-tech",
    )
    db_session.add(profile)
    db_session.flush()
    return profile


def test_record_batch_persists_pings_and_updates_presence(db_session):
    user = _user(db_session)
    _profile(db_session, user)
    now = datetime.now(UTC)

    result = field_location_tracking.record_batch(
        db_session,
        _auth(user),
        [
            {
                "latitude": 9.071,
                "longitude": 7.451,
                "accuracy_m": 10,
                "captured_at": now,
                "crm_work_order_id": "wo-live",
                "status": "on_shift",
            },
            {
                "latitude": 9.072,
                "longitude": 7.452,
                "captured_at": now + timedelta(minutes=1),
            },
        ],
    )

    assert result["accepted"] == 2
    assert result["errors"] == []
    assert result["presence"].status == "on_shift"
    assert result["presence"].last_latitude == 9.072
    assert (
        db_session.query(FieldTechLocationPing)
        .filter(FieldTechLocationPing.crm_work_order_id == "wo-live")
        .count()
        == 1
    )


def test_stale_ping_does_not_roll_presence_backwards(db_session):
    user = _user(db_session)
    _profile(db_session, user)
    now = datetime.now(UTC)
    auth = _auth(user)

    field_location_tracking.record_ping(
        db_session,
        auth,
        latitude=9.071,
        longitude=7.451,
        captured_at=now,
    )
    field_location_tracking.record_ping(
        db_session,
        auth,
        latitude=1.0,
        longitude=1.0,
        captured_at=now - timedelta(minutes=5),
    )

    presence = field_location_tracking.get_or_create_presence(db_session, auth)
    assert presence.last_latitude == 9.071
    assert db_session.query(FieldTechLocationPing).count() == 2


def test_location_batch_collects_per_ping_errors(db_session):
    user = _user(db_session)
    _profile(db_session, user)

    result = field_location_tracking.record_batch(
        db_session,
        _auth(user),
        [
            {"latitude": 9.071, "longitude": 7.451},
            {"latitude": 9.072, "longitude": 7.452, "status": "teleporting"},
        ],
    )

    assert result["accepted"] == 1
    assert result["errors"][0]["index"] == 1


def test_set_sharing_updates_presence_status(db_session):
    user = _user(db_session)
    _profile(db_session, user)

    presence = field_location_tracking.set_sharing(
        db_session,
        _auth(user),
        enabled=True,
        status="on_shift",
    )
    assert presence.location_sharing_enabled is True
    assert presence.status == "on_shift"

    presence = field_location_tracking.set_sharing(
        db_session, _auth(user), enabled=False
    )
    assert presence.location_sharing_enabled is False
    assert presence.status == "off_shift"


def test_unknown_status_is_rejected(db_session):
    user = _user(db_session)
    _profile(db_session, user)

    with pytest.raises(HTTPException) as exc:
        field_location_tracking.set_sharing(
            db_session,
            _auth(user),
            enabled=True,
            status="teleporting",
        )

    assert exc.value.status_code == 422


def test_location_api_routes(db_session):
    user = _user(db_session)
    _profile(db_session, user)
    app = FastAPI()
    app.include_router(router, prefix="/api/v1")
    app.dependency_overrides[get_db] = lambda: db_session
    app.dependency_overrides[require_user_auth] = lambda: _auth(user)
    client = TestClient(app)

    sharing = client.put(
        "/api/v1/field/locations/sharing",
        json={"enabled": True, "status": "on_shift"},
    )
    assert sharing.status_code == 200
    assert sharing.json()["status"] == "on_shift"

    ingest = client.post(
        "/api/v1/field/locations",
        json={"pings": [{"latitude": 9.071, "longitude": 7.451}]},
    )
    assert ingest.status_code == 200
    assert ingest.json()["accepted"] == 1

    presence = client.get("/api/v1/field/locations/me")
    assert presence.status_code == 200
    assert presence.json()["last_latitude"] == 9.071
