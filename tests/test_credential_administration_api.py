"""Transport checks for credential schema, actor and role admission."""

from types import SimpleNamespace
from uuid import uuid4

import pytest
from fastapi import FastAPI
from fastapi.routing import APIRoute
from fastapi.testclient import TestClient

from app.api import auth as api
from app.db import get_db
from app.errors import register_error_handlers
from app.models.rbac import Role
from app.services.auth_dependencies import require_user_auth


def app_for(auth):
    app = FastAPI()
    app.include_router(api.router, prefix="/api/v1")
    register_error_handlers(app)
    app.dependency_overrides[api._credential_admin.dependency] = lambda: auth
    app.dependency_overrides[api._credential_command_session] = lambda: None
    for route in api.router.routes:
        if isinstance(route, APIRoute):
            for dep in route.dependant.dependencies:
                if getattr(dep.call, "__name__", "") == "_require_permission":
                    app.dependency_overrides[dep.call] = lambda: auth
    return app


@pytest.mark.parametrize(
    "method,path",
    [
        ("POST", "/user-credentials"),
        ("PATCH", "/user-credentials/00000000-0000-0000-0000-000000000001"),
    ],
)
def test_forbidden_fields_return_422_without_echoing_secrets(method, path):
    app = app_for({"principal_id": str(uuid4())})
    payload = {
        "username": "valid",
        "password": "secret-password-never-echo",
        "password_hash": "secret-hash-never-echo",
    }
    if method == "POST":
        payload["subscriber_id"] = str(uuid4())
    response = TestClient(app).request(method, "/api/v1" + path, json=payload)
    assert response.status_code == 422
    assert "secret-password-never-echo" not in response.text
    assert "secret-hash-never-echo" not in response.text
    assert '"input":' not in response.text


def test_missing_trusted_actor_fails_closed():
    response = TestClient(app_for({})).post(
        "/api/v1/user-credentials",
        json={
            "subscriber_id": str(uuid4()),
            "username": "valid",
            "password": "Valid-Password-42!",
        },
    )
    assert response.status_code == 403


def test_non_admin_with_credential_permission_is_still_denied():
    auth = {
        "principal_id": str(uuid4()),
        "principal_type": "system_user",
        "roles": ["support"],
        "scopes": ["auth:credential:write"],
    }
    app = app_for(auth)
    del app.dependency_overrides[api._credential_admin.dependency]
    app.dependency_overrides[require_user_auth] = lambda: auth

    class Query:
        def __init__(self, model):
            self.model = model

        def filter(self, *args):
            return self

        def first(self):
            return SimpleNamespace(id=uuid4()) if self.model is Role else None

    class Db:
        def query(self, model):
            return Query(model)

    app.dependency_overrides[get_db] = lambda: Db()
    response = TestClient(app).delete(f"/api/v1/user-credentials/{uuid4()}")
    assert response.status_code == 403


def test_openapi_exposes_only_password_write_contract():
    app = app_for({})
    schemas = app.openapi()["components"]["schemas"]
    assert set(schemas["UserCredentialCreate"]["properties"]) == {
        "subscriber_id",
        "provider",
        "username",
        "radius_server_id",
        "password",
    }
    assert set(schemas["UserCredentialUpdate"]["properties"]) == {
        "username",
        "radius_server_id",
        "password",
    }
    assert schemas["UserCredentialUpdate"]["additionalProperties"] is False


def test_create_response_and_audit_use_trusted_actor(db_session, subscriber):
    from app.models.audit import AuditEvent

    actor = str(uuid4())
    app = app_for({"principal_id": actor})
    app.dependency_overrides[api._credential_command_session] = lambda: db_session
    identifier = subscriber.id
    db_session.commit()
    response = TestClient(app).post(
        "/api/v1/user-credentials",
        json={
            "subscriber_id": str(identifier),
            "username": "api-safe-response",
            "password": "Api-Strong-Password-42!",
        },
    )
    assert response.status_code == 201
    assert response.json()["username"] == "api-safe-response"
    assert "password_hash" not in response.json() and "password" not in response.json()
    assert db_session.query(AuditEvent).one().actor_id == actor


def test_lock_timeout_is_503_with_retry_after(monkeypatch):
    def busy(*args, **kwargs):
        raise api.credential_admin.error(
            "lock_timeout", "Credential is busy; retry shortly."
        )

    monkeypatch.setattr(api.credential_admin, "create_credential", busy)
    response = TestClient(app_for({"principal_id": str(uuid4())})).post(
        "/api/v1/user-credentials",
        json={
            "subscriber_id": str(uuid4()),
            "username": "busy",
            "password": "Valid-Password-42!",
        },
    )
    assert response.status_code == 503
    assert response.headers["Retry-After"] == "1"


@pytest.mark.parametrize(
    "password", ["Valid-42!" + "x" * 256, "Valid-42!" + chr(0xD800)]
)
def test_invalid_plaintext_returns_safe_422(db_session, subscriber, password):
    import json

    app = app_for({"principal_id": str(uuid4())})
    app.dependency_overrides[api._credential_command_session] = lambda: db_session
    identifier = subscriber.id
    db_session.commit()
    response = TestClient(app).post(
        "/api/v1/user-credentials",
        content=json.dumps(
            {
                "subscriber_id": str(identifier),
                "username": "bad-password",
                "password": password,
            },
            ensure_ascii=True,
        ),
        headers={"Content-Type": "application/json"},
    )
    assert response.status_code == 422
    assert "Valid-42!" not in response.text
    assert (
        "UnicodeEncodeError" not in response.text
        and "PasswordSizeError" not in response.text
    )
