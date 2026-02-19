import hashlib
from datetime import UTC, datetime, timedelta

import pyotp
import pytest
from cryptography.fernet import Fernet
from fastapi import HTTPException
from fastapi.routing import APIRoute
from starlette.requests import Request

from app.api.auth_flow import router as auth_flow_router
from app.models.auth import AuthProvider, SessionStatus, UserCredential
from app.models.auth import Session as AuthSession
from app.services.auth_dependencies import require_user_auth
from app.services.auth_flow import AuthFlow, hash_password


def _make_request(user_agent: str = "pytest"):
    scope = {
        "type": "http",
        "method": "POST",
        "path": "/auth",
        "headers": [(b"user-agent", user_agent.encode("utf-8"))],
        "client": ("127.0.0.1", 12345),
    }
    return Request(scope)

def _route_requires_auth(path: str) -> bool:
    for route in auth_flow_router.routes:
        if isinstance(route, APIRoute) and route.path == path:
            return any(
                dependency.call is require_user_auth
                for dependency in route.dependant.dependencies
            )
    raise AssertionError(f"Route not found: {path}")


def test_login_and_refresh_reuse_detection(db_session, person, monkeypatch):
    credential = UserCredential(
        person_id=person.id,
        provider=AuthProvider.local,
        username="user@example.com",
        password_hash=hash_password("secret"),
        is_active=True,
    )
    db_session.add(credential)
    db_session.commit()
    db_session.refresh(credential)

    request = _make_request()
    tokens = AuthFlow.login(db_session, "user@example.com", "secret", request, None)
    old_refresh = tokens["refresh_token"]

    rotated = AuthFlow.refresh(db_session, old_refresh, request)
    assert rotated["refresh_token"] != old_refresh

    with pytest.raises(HTTPException) as exc:
        AuthFlow.refresh(db_session, old_refresh, request)
    assert exc.value.status_code == 401
    assert "reuse" in str(exc.value.detail).lower()

    session = db_session.query(AuthSession).first()
    assert session.status == SessionStatus.revoked
    assert session.revoked_at is not None


def test_login_rejects_unsupported_provider(db_session, person):
    credential = UserCredential(
        person_id=person.id,
        provider=AuthProvider.local,
        username="user@example.com",
        password_hash=hash_password("secret"),
        is_active=True,
    )
    db_session.add(credential)
    db_session.commit()

    request = _make_request()
    with pytest.raises(HTTPException) as exc:
        AuthFlow.login(db_session, "user@example.com", "secret", request, "sso")
    assert exc.value.status_code == 400


def test_mfa_setup_confirm(db_session, person, monkeypatch):
    key = Fernet.generate_key().decode("utf-8")
    monkeypatch.setenv("TOTP_ENCRYPTION_KEY", key)
    monkeypatch.setenv("TOTP_ISSUER", "DotmacSM")

    credential = UserCredential(
        person_id=person.id,
        provider=AuthProvider.local,
        username="mfa@example.com",
        password_hash=hash_password("secret"),
        is_active=True,
    )
    db_session.add(credential)
    db_session.commit()

    setup = AuthFlow.mfa_setup(db_session, str(person.id), label="device")
    code = pyotp.TOTP(setup["secret"]).now()
    method = AuthFlow.mfa_confirm(db_session, str(setup["method_id"]), code, str(person.id))

    assert method.enabled is True
    assert method.is_primary is True
    assert method.is_active is True
    assert method.verified_at is not None


def test_mfa_setup_requires_auth():
    assert _route_requires_auth("/auth/mfa/setup") is True


def test_mfa_confirm_requires_auth():
    assert _route_requires_auth("/auth/mfa/confirm") is True


def test_login_lockout_after_failed_attempts(db_session, person, monkeypatch):
    monkeypatch.setenv("JWT_SECRET", "test-secret")
    credential = UserCredential(
        person_id=person.id,
        provider=AuthProvider.local,
        username="lockout@example.com",
        password_hash=hash_password("secret"),
        is_active=True,
    )
    db_session.add(credential)
    db_session.commit()

    request = _make_request()
    for _ in range(5):
        with pytest.raises(HTTPException) as exc:
            AuthFlow.login(db_session, "lockout@example.com", "wrong", request, None)
        assert exc.value.status_code == 401

    db_session.refresh(credential)
    assert credential.locked_until is not None

    with pytest.raises(HTTPException) as exc:
        AuthFlow.login(db_session, "lockout@example.com", "secret", request, None)
    assert exc.value.status_code == 403


def test_login_requires_password_reset(db_session, person, monkeypatch):
    monkeypatch.setenv("JWT_SECRET", "test-secret")
    credential = UserCredential(
        person_id=person.id,
        provider=AuthProvider.local,
        username="reset@example.com",
        password_hash=hash_password("secret"),
        must_change_password=True,
        is_active=True,
    )
    db_session.add(credential)
    db_session.commit()

    request = _make_request()
    with pytest.raises(HTTPException) as exc:
        AuthFlow.login(db_session, "reset@example.com", "secret", request, None)
    assert exc.value.status_code == 428


def test_login_returns_mfa_token_when_enabled(db_session, person, monkeypatch):
    monkeypatch.setenv("JWT_SECRET", "test-secret")
    monkeypatch.setenv("TOTP_ENCRYPTION_KEY", Fernet.generate_key().decode("utf-8"))
    credential = UserCredential(
        person_id=person.id,
        provider=AuthProvider.local,
        username="mfa-login@example.com",
        password_hash=hash_password("secret"),
        is_active=True,
    )
    db_session.add(credential)
    db_session.commit()

    setup = AuthFlow.mfa_setup(db_session, str(person.id), label="device")
    code = pyotp.TOTP(setup["secret"]).now()
    AuthFlow.mfa_confirm(db_session, str(setup["method_id"]), code, str(person.id))

    request = _make_request()
    result = AuthFlow.login(db_session, "mfa-login@example.com", "secret", request, None)
    assert result["mfa_required"] is True
    assert result["mfa_token"]


def test_refresh_expired_token_marks_session(db_session, person):
    refresh_token = "refresh-token"
    session = AuthSession(
        person_id=person.id,
        status=SessionStatus.active,
        token_hash=hashlib.sha256(refresh_token.encode("utf-8")).hexdigest(),
        expires_at=datetime.now(UTC) - timedelta(minutes=1),
    )
    db_session.add(session)
    db_session.commit()

    request = _make_request()
    with pytest.raises(HTTPException) as exc:
        AuthFlow.refresh(db_session, refresh_token, request)
    assert exc.value.status_code == 401
    db_session.refresh(session)
    assert session.status == SessionStatus.expired


def test_request_and_reset_password(db_session, person, monkeypatch):
    monkeypatch.setenv("JWT_SECRET", "test-secret")
    credential = UserCredential(
        person_id=person.id,
        provider=AuthProvider.local,
        username="reset-flow@example.com",
        password_hash=hash_password("secret"),
        is_active=True,
    )
    db_session.add(credential)
    db_session.commit()

    from app.services.auth_flow import request_password_reset, reset_password

    result = request_password_reset(db_session, person.email)
    assert result
    reset_at = reset_password(db_session, result["token"], "new-secret")
    assert isinstance(reset_at, datetime)
    db_session.refresh(credential)
    assert credential.must_change_password is False
    assert credential.failed_login_attempts == 0


def test_request_password_reset_unknown_email(db_session):
    from app.services.auth_flow import request_password_reset

    assert request_password_reset(db_session, "missing@example.com") is None


def test_reset_password_rejects_invalid_token(db_session, monkeypatch):
    monkeypatch.setenv("JWT_SECRET", "test-secret")
    from app.services.auth_flow import reset_password

    with pytest.raises(HTTPException) as exc:
        reset_password(db_session, "not-a-token", "secret")
    assert exc.value.status_code == 401
