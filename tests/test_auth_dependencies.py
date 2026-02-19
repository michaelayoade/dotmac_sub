import uuid
from datetime import UTC, datetime, timedelta

import pytest
from fastapi import HTTPException
from jose import jwt
from starlette.requests import Request

from app.models.auth import ApiKey, AuthProvider, SessionStatus, UserCredential
from app.models.auth import Session as AuthSession
from app.models.rbac import Permission, Role, RolePermission, SubscriberRole
from app.services import auth_dependencies as auth_dep
from app.services.auth import hash_api_key
from app.services.auth_dependencies import require_audit_auth, require_user_auth
from app.services.auth_flow import AuthFlow, hash_password, hash_session_token


def _make_access_token(person_id: str, session_id: str, scopes: list[str] | None = None, roles: list[str] | None = None):
    now = datetime.now(UTC)
    payload = {
        "sub": person_id,
        "session_id": session_id,
        "typ": "access",
        "iat": int(now.timestamp()),
        "exp": int((now + timedelta(minutes=15)).timestamp()),
    }
    if scopes:
        payload["scopes"] = scopes
    if roles:
        payload["roles"] = roles
    return jwt.encode(payload, "test-secret", algorithm="HS256")


def _make_request():
    scope = {
        "type": "http",
        "method": "POST",
        "path": "/auth",
        "headers": [(b"user-agent", b"pytest")],
        "client": ("127.0.0.1", 12345),
    }
    return Request(scope)


def test_require_user_auth_accepts_valid_token(db_session, person, monkeypatch):
    monkeypatch.setenv("JWT_SECRET", "test-secret")
    credential = UserCredential(
        subscriber_id=person.id,
        provider=AuthProvider.local,
        username="auth@example.com",
        password_hash=hash_password("secret"),
        is_active=True,
    )
    db_session.add(credential)
    db_session.commit()

    tokens = AuthFlow._issue_tokens(db_session, str(person.id), _make_request())
    auth = require_user_auth(authorization=f"Bearer {tokens['access_token']}", db=db_session)
    assert auth["subscriber_id"] == str(person.id)
    assert auth["session_id"]


def test_require_user_auth_rejects_expired_session(db_session, person, monkeypatch):
    monkeypatch.setenv("JWT_SECRET", "test-secret")
    session = AuthSession(
        subscriber_id=person.id,
        status=SessionStatus.active,
        token_hash="hash",
        expires_at=datetime.now(UTC) - timedelta(minutes=1),
    )
    db_session.add(session)
    db_session.commit()

    token = _make_access_token(str(person.id), str(session.id))
    with pytest.raises(HTTPException) as exc:
        require_user_auth(authorization=f"Bearer {token}", db=db_session)
    assert exc.value.status_code == 401


def test_require_audit_auth_accepts_api_key(db_session, person):
    raw_key = "raw-api-key"
    api_key = ApiKey(
        subscriber_id=person.id,
        label="test",
        key_hash=hash_api_key(raw_key),
        is_active=True,
        expires_at=datetime.now(UTC) + timedelta(days=1),
    )
    db_session.add(api_key)
    db_session.commit()

    auth = require_audit_auth(
        authorization=None,
        x_session_token=None,
        x_api_key=raw_key,
        db=db_session,
    )
    assert auth["actor_type"] == "api_key"


def test_require_audit_auth_requires_scope(db_session, person, monkeypatch):
    monkeypatch.setenv("JWT_SECRET", "test-secret")
    session = AuthSession(
        subscriber_id=person.id,
        status=SessionStatus.active,
        token_hash="hash",
        expires_at=datetime.now(UTC) + timedelta(days=1),
    )
    db_session.add(session)
    db_session.commit()

    token = _make_access_token(str(person.id), str(session.id))
    with pytest.raises(HTTPException) as exc:
        require_audit_auth(authorization=f"Bearer {token}", db=db_session)
    assert exc.value.status_code == 403


def test_require_audit_auth_accepts_scope(db_session, person, monkeypatch):
    monkeypatch.setenv("JWT_SECRET", "test-secret")
    session = AuthSession(
        subscriber_id=person.id,
        status=SessionStatus.active,
        token_hash="hash",
        expires_at=datetime.now(UTC) + timedelta(days=1),
    )
    db_session.add(session)
    db_session.commit()

    token = _make_access_token(str(person.id), str(session.id), scopes=["audit:read"])
    auth = require_audit_auth(authorization=f"Bearer {token}", db=db_session)
    assert auth["actor_type"] == "user"
    assert auth["actor_id"] == str(person.id)


def test_require_audit_auth_accepts_session_token(db_session, person):
    refresh_token = uuid.uuid4().hex
    session = AuthSession(
        subscriber_id=person.id,
        status=SessionStatus.active,
        token_hash=hash_session_token(refresh_token),
        expires_at=datetime.now(UTC) + timedelta(days=1),
    )
    db_session.add(session)
    db_session.commit()

    auth = require_audit_auth(authorization=None, x_session_token=refresh_token, db=db_session)
    assert auth["actor_type"] == "user"


def test_extract_bearer_and_scope_helpers():
    assert auth_dep._extract_bearer_token(None) is None
    assert auth_dep._extract_bearer_token("Bearer token") == "token"
    assert auth_dep._extract_bearer_token("Token token") is None
    assert auth_dep._is_jwt("a.b.c") is True
    assert auth_dep._is_jwt("token") is False
    assert auth_dep._has_audit_scope({"scope": "audit:read"}) is True
    assert auth_dep._has_audit_scope({"roles": ["admin"]}) is True
    assert auth_dep._has_audit_scope({"scopes": ["other"]}) is False
    naive = datetime.now()
    assert auth_dep._as_utc(naive).tzinfo is not None
    aware = datetime.now(UTC)
    assert auth_dep._as_utc(aware) == aware
    assert auth_dep._as_utc(None) is None
    assert auth_dep._has_audit_scope({"role": "admin"}) is True


def test_require_role_and_permission(db_session, person, monkeypatch):
    monkeypatch.setenv("JWT_SECRET", "test-secret")
    session = AuthSession(
        subscriber_id=person.id,
        status=SessionStatus.active,
        token_hash="hash",
        expires_at=datetime.now(UTC) + timedelta(days=1),
    )
    db_session.add(session)
    db_session.commit()

    token = _make_access_token(str(person.id), str(session.id), roles=["support"])
    auth = require_user_auth(authorization=f"Bearer {token}", db=db_session)

    role = Role(name="operator", is_active=True)
    permission = Permission(key="tickets:read", is_active=True)
    db_session.add_all([role, permission])
    db_session.commit()

    require_role = auth_dep.require_role("operator")
    with pytest.raises(HTTPException):
        require_role(auth=auth, db=db_session)

    link = SubscriberRole(subscriber_id=person.id, role_id=role.id)
    db_session.add(link)
    db_session.commit()
    assert require_role(auth=auth, db=db_session)["subscriber_id"] == str(person.id)

    require_permission = auth_dep.require_permission("tickets:read")
    with pytest.raises(HTTPException):
        require_permission(auth=auth, db=db_session)

    role_perm = RolePermission(role_id=role.id, permission_id=permission.id)
    db_session.add(role_perm)
    db_session.commit()
    assert require_permission(auth=auth, db=db_session)["subscriber_id"] == str(person.id)


def test_require_user_auth_missing_token(db_session):
    with pytest.raises(HTTPException):
        require_user_auth(authorization=None, db=db_session)


def test_require_user_auth_missing_claims(db_session, person, monkeypatch):
    monkeypatch.setenv("JWT_SECRET", "test-secret")
    token = jwt.encode(
        {"typ": "access", "iat": 1, "exp": 9999999999},
        "test-secret",
        algorithm="HS256",
    )
    with pytest.raises(HTTPException):
        require_user_auth(authorization=f"Bearer {token}", db=db_session)


def test_require_audit_auth_invalid_session(db_session, person, monkeypatch):
    monkeypatch.setenv("JWT_SECRET", "test-secret")
    token = _make_access_token(str(person.id), str(uuid.uuid4()), scopes=["audit:read"])
    with pytest.raises(HTTPException):
        require_audit_auth(authorization=f"Bearer {token}", db=db_session)


def test_require_audit_auth_revoked_session(db_session, person, monkeypatch):
    monkeypatch.setenv("JWT_SECRET", "test-secret")
    session = AuthSession(
        subscriber_id=person.id,
        status=SessionStatus.active,
        token_hash="hash",
        revoked_at=datetime.now(UTC),
        expires_at=datetime.now(UTC) + timedelta(days=1),
    )
    db_session.add(session)
    db_session.commit()
    token = _make_access_token(str(person.id), str(session.id), scopes=["audit:read"])
    with pytest.raises(HTTPException):
        require_audit_auth(authorization=f"Bearer {token}", db=db_session)


def test_require_audit_auth_sets_actor_id(db_session, person, monkeypatch):
    monkeypatch.setenv("JWT_SECRET", "test-secret")
    session = AuthSession(
        subscriber_id=person.id,
        status=SessionStatus.active,
        token_hash="hash",
        expires_at=datetime.now(UTC) + timedelta(days=1),
    )
    db_session.add(session)
    db_session.commit()
    token = _make_access_token(str(person.id), str(session.id), scopes=["audit:read"])
    request = Request({"type": "http", "headers": []})
    auth = require_audit_auth(authorization=f"Bearer {token}", request=request, db=db_session)
    assert request.state.actor_id == str(person.id)
    assert auth["actor_type"] == "user"


def test_require_audit_auth_session_token_sets_actor_id(db_session, person):
    refresh_token = uuid.uuid4().hex
    session = AuthSession(
        subscriber_id=person.id,
        status=SessionStatus.active,
        token_hash=hash_session_token(refresh_token),
        expires_at=datetime.now(UTC) + timedelta(days=1),
    )
    db_session.add(session)
    db_session.commit()
    request = Request({"type": "http", "headers": []})
    auth = require_audit_auth(
        authorization=None, x_session_token=refresh_token, request=request, db=db_session
    )
    assert request.state.actor_id == str(person.id)
    assert auth["actor_type"] == "user"


def test_require_audit_auth_api_key_sets_actor_id(db_session, person):
    raw_key = "raw-key"
    api_key = ApiKey(
        subscriber_id=person.id,
        label="test",
        key_hash=hash_api_key(raw_key),
        is_active=True,
        expires_at=datetime.now(UTC) + timedelta(days=1),
    )
    db_session.add(api_key)
    db_session.commit()
    request = Request({"type": "http", "headers": []})
    auth = require_audit_auth(
        authorization=None, x_session_token=None, x_api_key=raw_key, request=request, db=db_session
    )
    assert request.state.actor_id == str(api_key.id)
    assert auth["actor_type"] == "api_key"


def test_require_role_missing_role(db_session, person, monkeypatch):
    monkeypatch.setenv("JWT_SECRET", "test-secret")
    session = AuthSession(
        subscriber_id=person.id,
        status=SessionStatus.active,
        token_hash="hash",
        expires_at=datetime.now(UTC) + timedelta(days=1),
    )
    db_session.add(session)
    db_session.commit()
    token = _make_access_token(str(person.id), str(session.id), roles=["support"])
    auth = require_user_auth(authorization=f"Bearer {token}", db=db_session)

    require_role = auth_dep.require_role("missing")
    with pytest.raises(HTTPException):
        require_role(auth=auth, db=db_session)


def test_require_role_short_circuit(db_session, person, monkeypatch):
    monkeypatch.setenv("JWT_SECRET", "test-secret")
    session = AuthSession(
        subscriber_id=person.id,
        status=SessionStatus.active,
        token_hash="hash",
        expires_at=datetime.now(UTC) + timedelta(days=1),
    )
    db_session.add(session)
    db_session.commit()
    token = _make_access_token(str(person.id), str(session.id), roles=["admin"])
    auth = require_user_auth(authorization=f"Bearer {token}", db=db_session)
    require_role = auth_dep.require_role("admin")
    assert require_role(auth=auth, db=db_session)["subscriber_id"] == str(person.id)


def test_require_permission_missing_permission(db_session, person, monkeypatch):
    monkeypatch.setenv("JWT_SECRET", "test-secret")
    session = AuthSession(
        subscriber_id=person.id,
        status=SessionStatus.active,
        token_hash="hash",
        expires_at=datetime.now(UTC) + timedelta(days=1),
    )
    db_session.add(session)
    db_session.commit()
    token = _make_access_token(str(person.id), str(session.id), roles=["support"])
    auth = require_user_auth(authorization=f"Bearer {token}", db=db_session)

    require_permission = auth_dep.require_permission("missing:perm")
    with pytest.raises(HTTPException):
        require_permission(auth=auth, db=db_session)


def test_require_permission_short_circuit(db_session, person, monkeypatch):
    monkeypatch.setenv("JWT_SECRET", "test-secret")
    session = AuthSession(
        subscriber_id=person.id,
        status=SessionStatus.active,
        token_hash="hash",
        expires_at=datetime.now(UTC) + timedelta(days=1),
    )
    db_session.add(session)
    db_session.commit()
    token = _make_access_token(str(person.id), str(session.id), roles=["admin"])
    auth = require_user_auth(authorization=f"Bearer {token}", db=db_session)
    require_permission = auth_dep.require_permission("any:perm")
    assert require_permission(auth=auth, db=db_session)["subscriber_id"] == str(person.id)
