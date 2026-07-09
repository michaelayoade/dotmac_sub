from __future__ import annotations

from uuid import uuid4

from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api.field import router
from app.db import get_db
from app.models.subscriber import UserType
from app.models.system_user import SystemUser
from app.services.auth_dependencies import require_user_auth


def _user(db_session) -> SystemUser:
    user = SystemUser(
        first_name="Ade",
        last_name="Tech",
        email=f"ade-{uuid4().hex[:8]}@example.com",
        user_type=UserType.system_user,
    )
    db_session.add(user)
    db_session.commit()
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


def _client(db_session, auth: dict) -> TestClient:
    app = FastAPI()
    app.include_router(router, prefix="/api/v1")
    app.dependency_overrides[get_db] = lambda: db_session
    app.dependency_overrides[require_user_auth] = lambda: auth
    return TestClient(app)


def test_field_device_register_list_and_delete(db_session):
    user = _user(db_session)
    client = _client(db_session, _auth(user))

    created = client.post(
        "/api/v1/field/devices",
        json={
            "platform": "android",
            "fcm_token": "field-token",
            "app_version": "1.0.1",
        },
    )
    assert created.status_code == 201
    assert created.json()["system_user_id"] == str(user.id)
    assert created.json()["app_version"] == "1.0.1"
    assert "field-token" not in created.text
    device_id = created.json()["id"]

    listed = client.get("/api/v1/field/devices")
    assert listed.status_code == 200
    assert listed.json()["count"] == 1
    assert listed.json()["items"][0]["id"] == device_id

    deleted = client.delete(f"/api/v1/field/devices/{device_id}")
    assert deleted.status_code == 204
    assert client.get("/api/v1/field/devices").json()["count"] == 0


def test_field_device_rejects_subscriber_principal(db_session):
    client = _client(
        db_session,
        {
            "principal_id": str(uuid4()),
            "person_id": str(uuid4()),
            "subscriber_id": str(uuid4()),
            "principal_type": "subscriber",
            "roles": [],
            "scopes": [],
        },
    )

    resp = client.post(
        "/api/v1/field/devices",
        json={"platform": "android", "fcm_token": "field-token"},
    )

    assert resp.status_code == 403
