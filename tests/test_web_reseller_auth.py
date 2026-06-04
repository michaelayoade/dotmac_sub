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
    reset_request.assert_called_once_with(db=db, email="reseller@example.com")


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
