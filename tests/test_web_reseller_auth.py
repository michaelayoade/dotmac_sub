from __future__ import annotations

from unittest.mock import MagicMock

from fastapi import HTTPException
from starlette.requests import Request
from starlette.responses import RedirectResponse

from app.services import web_reseller_auth as web_reseller_auth_service


def _request() -> Request:
    return Request(
        {
            "type": "http",
            "method": "POST",
            "path": "/reseller/auth/login",
            "headers": [],
            "query_string": b"",
        }
    )


def test_invalidate_session_revokes_backing_auth_session(db_session, person):
    """Logout revokes the underlying auth_flow session, not just the local one."""
    from datetime import UTC, datetime, timedelta

    from app.models.auth import Session as AuthSession
    from app.models.auth import SessionStatus
    from app.services import reseller_portal

    auth_session = AuthSession(
        subscriber_id=person.id,
        status=SessionStatus.active,
        token_hash="reseller-logout-token",
        expires_at=datetime.now(UTC) + timedelta(hours=1),
    )
    db_session.add(auth_session)
    db_session.commit()

    token = reseller_portal._create_session(
        username="reseller@example.com",
        subscriber_id=str(person.id),
        reseller_id=str(person.id),
        remember=False,
        db=db_session,
        auth_session_id=str(auth_session.id),
    )

    reseller_portal.invalidate_session(token, db_session)

    db_session.refresh(auth_session)
    assert auth_session.status == SessionStatus.revoked
    # Local portal session is gone too.
    assert reseller_portal._get_session(token) is None


def test_password_reset_email_for_identifier_uses_local_username(db_session, person):
    from app.models.auth import AuthProvider, UserCredential

    person.email = "reseller@example.com"
    db_session.commit()

    credential = UserCredential(
        subscriber_id=person.id,
        provider=AuthProvider.local,
        username="reseller-username",
        password_hash="hash",
        is_active=True,
    )
    db_session.add(credential)
    db_session.commit()

    result = web_reseller_auth_service._password_reset_email_for_identifier(
        db_session, "reseller-username"
    )

    assert result == "reseller@example.com"


def test_reseller_login_redirects_to_shared_reset_flow(monkeypatch):
    request = _request()
    db = object()

    def _raise_password_reset_required(*_args, **_kwargs):
        raise HTTPException(
            status_code=428,
            detail={
                "code": "PASSWORD_RESET_REQUIRED",
                "message": "Password reset required",
            },
        )

    monkeypatch.setattr(
        web_reseller_auth_service.reseller_portal,
        "login",
        _raise_password_reset_required,
    )
    monkeypatch.setattr(
        web_reseller_auth_service,
        "_password_reset_email_for_identifier",
        lambda _db, _identifier: "reseller@example.com",
    )
    reset_request = MagicMock(
        return_value={"token": "reset-token", "email": "reseller@example.com"}
    )
    monkeypatch.setattr(
        web_reseller_auth_service.auth_flow_service,
        "request_password_reset",
        reset_request,
    )

    response = web_reseller_auth_service.reseller_login_submit(
        request,
        db,
        "reseller@example.com",
        "secret",
        remember=False,
    )

    assert isinstance(response, RedirectResponse)
    assert response.status_code == 303
    assert response.headers["location"] == (
        "/auth/reset-password?token=reset-token&next_login="
        "%2Freseller%2Fauth%2Flogin%3Fnext%3D%2Freseller%2Fdashboard"
    )
    reset_request.assert_called_once_with(
        db=db, email="reseller@example.com", ttl_minutes=15
    )


def test_reseller_login_returns_error_when_reset_token_not_generated(monkeypatch):
    request = _request()
    db = object()

    def _raise_password_reset_required(*_args, **_kwargs):
        raise HTTPException(
            status_code=428,
            detail={
                "code": "PASSWORD_RESET_REQUIRED",
                "message": "Password reset required",
            },
        )

    monkeypatch.setattr(
        web_reseller_auth_service.reseller_portal,
        "login",
        _raise_password_reset_required,
    )
    monkeypatch.setattr(
        web_reseller_auth_service,
        "_password_reset_email_for_identifier",
        lambda _db, _identifier: "reseller@example.com",
    )
    monkeypatch.setattr(
        web_reseller_auth_service.auth_flow_service,
        "request_password_reset",
        lambda **_kwargs: None,
    )

    response = web_reseller_auth_service.reseller_login_submit(
        request,
        db,
        "reseller@example.com",
        "secret",
        remember=False,
    )

    assert response.status_code == 401
    assert "Password reset required" in response.body.decode()


def test_reseller_login_error_hides_http_status_prefix(monkeypatch):
    request = _request()
    db = object()

    def _raise_invalid_credentials(*_args, **_kwargs):
        raise HTTPException(status_code=401, detail="Invalid credentials")

    monkeypatch.setattr(
        web_reseller_auth_service.reseller_portal,
        "login",
        _raise_invalid_credentials,
    )

    response = web_reseller_auth_service.reseller_login_submit(
        request,
        db,
        "reseller@example.com",
        "wrong-password",
        remember=False,
    )

    body = response.body.decode()
    assert response.status_code == 401
    assert "Wrong email/username or password." in body
    assert "401: Invalid credentials" not in body


def test_reseller_forgot_password_sends_reseller_reset_link(monkeypatch):
    request = _request()
    db = object()
    forgot_flow = MagicMock()
    monkeypatch.setattr(
        web_reseller_auth_service.auth_flow_service,
        "forgot_password_flow",
        forgot_flow,
    )

    response = web_reseller_auth_service.reseller_forgot_password_submit(
        request,
        db,
        "reseller@example.com",
    )

    assert response.status_code == 200
    assert "Check your email" in response.body.decode()
    forgot_flow.assert_called_once_with(
        db,
        "reseller@example.com",
        next_login_path="/reseller/auth/login?next=/reseller/dashboard",
    )
