"""Authorization, atomicity and audit evidence for MFA method revocation."""

from types import SimpleNamespace
from uuid import uuid4

import pytest
from fastapi import FastAPI, HTTPException
from fastapi.routing import APIRoute
from fastapi.testclient import TestClient

from app.api import auth as api
from app.db import get_db
from app.errors import register_error_handlers
from app.models.audit import AuditEvent
from app.models.auth import MFAMethod, MFAMethodType, MFARecoveryCode
from app.models.rbac import Role
from app.services import auth as auth_service
from app.services.auth_dependencies import require_user_auth

ADMIN = {"principal_id": str(uuid4()), "principal_type": "system_user"}


def _app(auth, db):
    app = FastAPI()
    app.include_router(api.router, prefix="/api/v1")
    register_error_handlers(app)
    app.dependency_overrides[api._credential_admin.dependency] = lambda: auth
    app.dependency_overrides[get_db] = lambda: db
    for route in api.router.routes:
        if isinstance(route, APIRoute):
            for dep in route.dependant.dependencies:
                if getattr(dep.call, "__name__", "") == "_require_permission":
                    app.dependency_overrides[dep.call] = lambda: auth
    return app


def _method(db, subscriber, *, primary=True, codes=2):
    method = MFAMethod(
        subscriber_id=subscriber.id,
        method_type=MFAMethodType.totp,
        label="m",
        secret="unit-fixture-secret",
        is_primary=primary,
        enabled=True,
    )
    db.add(method)
    db.flush()
    for _ in range(codes):
        db.add(MFARecoveryCode(mfa_method_id=method.id, code_hash=uuid4().hex))
    db.commit()
    return method


def test_route_requires_credential_write_permission_and_admin_role():
    route = next(
        r
        for r in api.router.routes
        if isinstance(r, APIRoute)
        and r.path == "/mfa-methods/{method_id}"
        and "DELETE" in r.methods
    )
    names = [getattr(d.call, "__name__", "") for d in route.dependant.dependencies]
    assert "_require_permission" in names
    assert "_require_role" in names


def test_non_admin_with_permission_is_denied_and_nothing_changes(
    db_session, subscriber
):
    method = _method(db_session, subscriber)
    mid = str(method.id)
    auth = {
        "principal_id": str(uuid4()),
        "principal_type": "system_user",
        "roles": ["support"],
        "scopes": ["auth:credential:write"],
    }
    app = _app(auth, db_session)
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
    assert TestClient(app).delete(f"/api/v1/mfa-methods/{mid}").status_code == 403
    assert db_session.get(MFAMethod, method.id).is_active is True


def test_missing_actor_identity_fails_closed(db_session, subscriber):
    method = _method(db_session, subscriber)
    mid = str(method.id)
    response = TestClient(_app({}, db_session)).delete(f"/api/v1/mfa-methods/{mid}")
    assert response.status_code == 403
    assert db_session.get(MFAMethod, method.id).is_active is True


def test_admin_revocation_disables_method_codes_and_audits_atomically(
    db_session, subscriber
):
    primary = _method(db_session, subscriber, primary=True, codes=2)
    other = _method(db_session, subscriber, primary=False, codes=1)
    pid, oid, sid = str(primary.id), other.id, str(subscriber.id)
    response = TestClient(_app(ADMIN, db_session)).delete(f"/api/v1/mfa-methods/{pid}")
    assert response.status_code == 204
    db_session.expire_all()
    row = db_session.get(MFAMethod, primary.id)
    assert (row.is_active, row.enabled, row.is_primary) == (False, False, False)
    # Recovery codes are keyed to the method: this method's codes die with it,
    # a different method's codes are untouched.
    assert (
        db_session.query(MFARecoveryCode)
        .filter(
            MFARecoveryCode.mfa_method_id == primary.id,
            MFARecoveryCode.is_active.is_(True),
        )
        .count()
        == 0
    )
    assert (
        db_session.query(MFARecoveryCode)
        .filter(
            MFARecoveryCode.mfa_method_id == oid, MFARecoveryCode.is_active.is_(True)
        )
        .count()
        == 1
    )
    event = db_session.query(AuditEvent).one()
    assert event.action == "auth.mfa_method.disabled"
    assert event.actor_id == ADMIN["principal_id"]
    assert event.entity_id == pid
    assert event.metadata_["principal_type"] == "subscriber"
    assert event.metadata_["principal_id"] == sid
    assert event.metadata_["recovery_codes_deactivated"] == 2
    assert "unit-fixture-secret" not in repr(event.metadata_)


def test_repeat_revocation_is_a_noop_without_second_audit(db_session, subscriber):
    method = _method(db_session, subscriber)
    mid = str(method.id)
    client = TestClient(_app(ADMIN, db_session))
    assert client.delete(f"/api/v1/mfa-methods/{mid}").status_code == 204
    assert client.delete(f"/api/v1/mfa-methods/{mid}").status_code == 204
    assert db_session.query(AuditEvent).count() == 1


def test_unknown_method_is_404_without_audit(db_session):
    response = TestClient(_app(ADMIN, db_session)).delete(
        f"/api/v1/mfa-methods/{uuid4()}"
    )
    assert response.status_code == 404
    assert db_session.query(AuditEvent).count() == 0


def test_audit_failure_rolls_back_the_state_change(monkeypatch, db_session, subscriber):
    method = _method(db_session, subscriber)
    mid = method.id

    def boom(*args, **kwargs):
        raise RuntimeError("audit unavailable")

    monkeypatch.setattr(auth_service, "stage_audit_event", boom)
    with pytest.raises(RuntimeError):
        auth_service.mfa_methods.delete(db_session, str(mid), actor_id="admin")
    db_session.expire_all()
    row = db_session.get(MFAMethod, mid)
    assert (row.is_active, row.enabled, row.is_primary) == (True, True, True)
    assert (
        db_session.query(MFARecoveryCode)
        .filter(MFARecoveryCode.mfa_method_id == mid, MFARecoveryCode.is_active)
        .count()
        == 2
    )


def test_service_refuses_blank_actor(db_session, subscriber):
    method = _method(db_session, subscriber)
    with pytest.raises(HTTPException) as exc:
        auth_service.mfa_methods.delete(db_session, str(method.id), actor_id=" ")
    assert exc.value.status_code == 403
