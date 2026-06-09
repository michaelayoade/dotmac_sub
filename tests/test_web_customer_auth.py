from http.cookies import SimpleCookie

import pyotp
from cryptography.fernet import Fernet
from starlette.requests import Request

from app.models.auth import AuthProvider, UserCredential
from app.services import customer_portal
from app.services import web_customer_auth as web_customer_auth_service
from app.services.auth_flow import AuthFlow, hash_password


def _request_with_cookie(name: str, value: str) -> Request:
    return _request(cookies={name: value})


def _request(cookies: dict[str, str] | None = None) -> Request:
    headers = []
    if cookies:
        headers.append(
            (
                b"cookie",
                "; ".join(f"{key}={value}" for key, value in cookies.items()).encode(),
            )
        )
    scope = {
        "type": "http",
        "method": "GET",
        "path": "/portal",
        "headers": headers,
    }
    request = Request(scope)
    request.state.csrf_token = "csrf"
    return request


def _response_cookies(response) -> dict[str, str]:
    jar = SimpleCookie()
    for header, value in response.raw_headers:
        if header.lower() == b"set-cookie":
            jar.load(value.decode())
    return {key: morsel.value for key, morsel in jar.items()}


def test_get_current_customer_enriches_module_flags(monkeypatch):
    monkeypatch.setattr(
        web_customer_auth_service.customer_portal,
        "get_current_customer",
        lambda _token, _db: {"username": "alice"},
    )
    monkeypatch.setattr(
        web_customer_auth_service.module_manager_service,
        "load_module_states",
        lambda _db: {"billing": False},
    )
    monkeypatch.setattr(
        web_customer_auth_service.module_manager_service,
        "load_feature_states",
        lambda _db: {"services_view": False},
    )

    request = _request_with_cookie(
        web_customer_auth_service.customer_portal.SESSION_COOKIE_NAME,
        "session-token",
    )
    current = web_customer_auth_service.get_current_customer_from_request(
        request, db=object()
    )

    assert current is not None
    assert current["username"] == "alice"
    assert current["module_states"]["billing"] is False
    assert current["feature_states"]["services_view"] is False


def test_get_current_customer_returns_none_when_session_missing(monkeypatch):
    monkeypatch.setattr(
        web_customer_auth_service.customer_portal,
        "get_current_customer",
        lambda _token, _db: None,
    )
    request = _request_with_cookie(
        web_customer_auth_service.customer_portal.SESSION_COOKIE_NAME,
        "missing",
    )
    assert (
        web_customer_auth_service.get_current_customer_from_request(
            request, db=object()
        )
        is None
    )


def test_customer_session_can_store_impersonation_marker(db_session, subscriber):
    token = customer_portal.create_customer_session(
        username="impersonate:reseller:test",
        account_id=subscriber.id,
        subscriber_id=subscriber.id,
        return_to="/reseller/accounts",
        is_impersonation=True,
        db=db_session,
    )

    session = customer_portal.get_customer_session(token)

    assert session is not None
    assert session["is_impersonation"] is True
    assert session["return_to"] == "/reseller/accounts"


def test_customer_login_redirects_to_mfa_when_enabled(
    db_session, subscriber, monkeypatch
):
    monkeypatch.setenv("JWT_SECRET", "test-secret")
    monkeypatch.setenv("TOTP_ENCRYPTION_KEY", Fernet.generate_key().decode("utf-8"))
    credential = UserCredential(
        subscriber_id=subscriber.id,
        provider=AuthProvider.local,
        username="mfa-customer@example.com",
        password_hash=hash_password("secret"),
        is_active=True,
    )
    db_session.add(credential)
    db_session.commit()

    setup = AuthFlow.mfa_setup(db_session, str(subscriber.id), label="device")
    AuthFlow.mfa_confirm(
        db_session,
        str(setup["method_id"]),
        pyotp.TOTP(setup["secret"]).now(),
        str(subscriber.id),
    )

    response = web_customer_auth_service.customer_login_submit(
        _request(),
        db_session,
        "mfa-customer@example.com",
        "secret",
        True,
        "/portal/billing",
    )

    assert response.status_code == 303
    assert response.headers["location"] == "/portal/auth/mfa"
    cookies = _response_cookies(response)
    assert "customer_mfa_pending" in cookies
    assert "customer_mfa_context" in cookies
    assert customer_portal.SESSION_COOKIE_NAME not in cookies


def test_customer_mfa_submit_creates_customer_session(
    db_session, subscriber, monkeypatch
):
    monkeypatch.setenv("JWT_SECRET", "test-secret")
    monkeypatch.setenv("TOTP_ENCRYPTION_KEY", Fernet.generate_key().decode("utf-8"))
    credential = UserCredential(
        subscriber_id=subscriber.id,
        provider=AuthProvider.local,
        username="mfa-submit@example.com",
        password_hash=hash_password("secret"),
        is_active=True,
    )
    db_session.add(credential)
    db_session.commit()

    setup = AuthFlow.mfa_setup(db_session, str(subscriber.id), label="device")
    AuthFlow.mfa_confirm(
        db_session,
        str(setup["method_id"]),
        pyotp.TOTP(setup["secret"]).now(),
        str(subscriber.id),
    )
    login_response = web_customer_auth_service.customer_login_submit(
        _request(),
        db_session,
        "mfa-submit@example.com",
        "secret",
        False,
        "/portal/profile",
    )
    pending_cookies = _response_cookies(login_response)

    invalid_response = web_customer_auth_service.customer_mfa_submit(
        _request(cookies=pending_cookies),
        db_session,
        "000000",
    )
    assert invalid_response.status_code == 401

    valid_response = web_customer_auth_service.customer_mfa_submit(
        _request(cookies=pending_cookies),
        db_session,
        pyotp.TOTP(setup["secret"]).now(),
    )

    assert valid_response.status_code == 303
    assert valid_response.headers["location"] == "/portal/profile"
    cookies = _response_cookies(valid_response)
    assert customer_portal.SESSION_COOKIE_NAME in cookies
    assert customer_portal.get_customer_session(
        cookies[customer_portal.SESSION_COOKIE_NAME]
    )
