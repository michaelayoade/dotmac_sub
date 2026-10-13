from datetime import UTC, datetime, timedelta
from http.cookies import SimpleCookie
from pathlib import Path
from types import SimpleNamespace
from uuid import UUID

import pyotp
import pytest
from cryptography.fernet import Fernet
from starlette.requests import Request

from app.models.auth import AuthProvider, UserCredential
from app.models.catalog import AccessCredential
from app.models.subscriber import Subscriber, SubscriberStatus, UserType
from app.models.system_user import SystemUser
from app.services import customer_portal
from app.services import web_customer_auth as web_customer_auth_service
from app.services.auth_flow import hash_password
from tests.authentication_denial_unit import (
    authentication_denial_unit_session as authentication_denial_unit_session,
)
from tests.subscriber_mfa_fixtures import seed_enabled_subscriber_mfa

pytestmark = pytest.mark.usefixtures("authentication_denial_unit_session")


@pytest.fixture
def portal_successes(monkeypatch):
    observed = []
    monkeypatch.setattr(
        web_customer_auth_service,
        "record_portal_authentication_success",
        lambda *, src: observed.append(src),
    )
    return observed


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
        "query_string": b"",
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


def test_customer_login_template_has_submit_loading_state():
    template = Path("templates/customer/auth/login.html").read_text()

    assert 'x-data="{ loading: false }"' in template
    assert 'x-on:submit="loading = true"' in template
    assert ':disabled="loading"' in template
    assert "loading ? 'Signing in...' : 'Sign in'" in template


def test_customer_mfa_template_has_submit_loading_state():
    template = Path("templates/customer/auth/mfa.html").read_text()

    assert "x-data=\"{ loading: false, code: '' }\"" in template
    assert 'x-on:submit="loading = true"' in template
    assert 'x-model="code"' in template
    assert ':disabled="loading || code.length < 6"' in template
    assert "Verification or recovery code" in template
    assert "loading ? 'Verifying...' : 'Verify'" in template


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


def test_customer_impersonation_banner_uses_recoverable_exit_link():
    template = Path("templates/layouts/customer.html").read_text()

    assert (
        'href="/portal/auth/stop-impersonation?next={{ impersonation_return_to | urlencode }}"'
        in template
    )
    assert 'method="post" action="/portal/auth/stop-impersonation"' not in template


def test_customer_mobile_header_cannot_expand_the_document_width():
    template = Path("templates/layouts/customer.html").read_text()

    assert "min-h-screen min-w-0 flex-col overflow-x-clip" in template
    assert 'class="group flex min-w-0 flex-1 items-center' in template
    assert "min-w-0 truncate font-display text-base" in template
    assert 'class="flex shrink-0 items-center gap-1 sm:gap-2"' in template
    assert 'class="relative min-w-0 flex-1 mesh-gradient"' in template


def test_customer_stop_impersonation_invalidates_session_and_returns_to_admin(
    monkeypatch,
):
    invalidated: list[str] = []
    monkeypatch.setattr(
        web_customer_auth_service.customer_portal,
        "invalidate_customer_session",
        invalidated.append,
    )
    request = _request_with_cookie(
        web_customer_auth_service.customer_portal.SESSION_COOKIE_NAME,
        "impersonation-session",
    )

    response = web_customer_auth_service.customer_stop_impersonation(
        request,
        "/admin/customers/person/customer-id",
    )

    assert invalidated == ["impersonation-session"]
    assert response.status_code == 303
    assert response.headers["location"] == "/admin/customers/person/customer-id"
    assert (
        _response_cookies(response)[
            web_customer_auth_service.customer_portal.SESSION_COOKIE_NAME
        ]
        == ""
    )


def test_customer_login_allows_pppoe_when_local_credential_password_differs(
    db_session, subscriber, monkeypatch, portal_successes
):
    monkeypatch.setattr(
        web_customer_auth_service.radius_auth,
        "authenticate",
        lambda **_kwargs: (_ for _ in ()).throw(RuntimeError("radius unavailable")),
    )
    local_credential = UserCredential(
        subscriber_id=subscriber.id,
        provider=AuthProvider.local,
        username="100024880",
        password_hash=hash_password("portal-secret"),
        is_active=True,
        must_change_password=True,
    )
    access_credential = AccessCredential(
        subscriber_id=subscriber.id,
        username="100024880",
        secret_hash="plain:pppoe-secret",
        is_active=True,
    )
    db_session.add_all([local_credential, access_credential])
    db_session.commit()

    response = web_customer_auth_service.customer_login_submit(
        _request(),
        db_session,
        "100024880",
        "pppoe-secret",
        False,
        "/portal/dashboard",
    )

    db_session.refresh(local_credential)
    assert response.status_code == 303
    assert response.headers["location"] == "/portal/dashboard"
    assert customer_portal.SESSION_COOKIE_NAME in _response_cookies(response)
    assert local_credential.failed_login_attempts == 0
    assert portal_successes == ["access_credential"]


def test_customer_login_ignores_staff_local_credential(db_session, monkeypatch):
    monkeypatch.setattr(
        web_customer_auth_service.radius_auth,
        "authenticate",
        lambda **_kwargs: (_ for _ in ()).throw(RuntimeError("radius unavailable")),
    )
    staff = SystemUser(
        first_name="Portal",
        last_name="Collision",
        email="portal-collision@example.com",
        user_type=UserType.system_user,
        is_active=True,
    )
    db_session.add(staff)
    db_session.flush()
    credential = UserCredential(
        system_user_id=staff.id,
        provider=AuthProvider.local,
        username=staff.email,
        password_hash=hash_password("staff-secret"),
        is_active=True,
    )
    db_session.add(credential)
    db_session.commit()

    response = web_customer_auth_service.customer_login_submit(
        _request(),
        db_session,
        staff.email,
        "staff-secret",
        False,
        "/portal/dashboard",
    )

    db_session.refresh(credential)
    assert response.status_code == 401
    assert credential.last_login_at is None


def test_customer_login_unique_email_uses_portal_password(
    db_session, subscriber, monkeypatch
):
    monkeypatch.setattr(
        web_customer_auth_service.radius_auth,
        "authenticate",
        lambda **_kwargs: (_ for _ in ()).throw(RuntimeError("radius unavailable")),
    )
    subscriber.email = "Portal.Customer@Example.com"
    credential = UserCredential(
        subscriber_id=subscriber.id,
        provider=AuthProvider.local,
        username="105000110",
        password_hash=hash_password("portal-secret"),
        is_active=True,
    )
    db_session.add(credential)
    db_session.commit()

    response = web_customer_auth_service.customer_login_submit(
        _request(),
        db_session,
        "portal.customer@example.COM",
        "portal-secret",
        False,
        "/portal/dashboard",
    )

    assert response.status_code == 303
    assert customer_portal.SESSION_COOKIE_NAME in _response_cookies(response)
    assert credential.username == "105000110"


def test_customer_login_shared_email_asks_for_customer_number(
    db_session, subscriber, monkeypatch
):
    monkeypatch.setattr(
        web_customer_auth_service.radius_auth,
        "authenticate",
        lambda **_kwargs: (_ for _ in ()).throw(RuntimeError("radius unavailable")),
    )
    shared_email = "shared-portal@example.com"
    subscriber.email = shared_email
    other = Subscriber(
        first_name="Shared",
        last_name="Customer",
        email=shared_email,
        status=SubscriberStatus.active,
        is_active=True,
        reseller_id=subscriber.reseller_id,
    )
    db_session.add(other)
    db_session.flush()
    db_session.add_all(
        [
            UserCredential(
                subscriber_id=subscriber.id,
                provider=AuthProvider.local,
                username="105000111",
                password_hash=hash_password("portal-secret"),
                is_active=True,
            ),
            UserCredential(
                subscriber_id=other.id,
                provider=AuthProvider.local,
                username="105000112",
                password_hash=hash_password("portal-secret"),
                is_active=True,
            ),
        ]
    )
    db_session.commit()

    response = web_customer_auth_service.customer_login_submit(
        _request(),
        db_session,
        shared_email,
        "portal-secret",
        False,
        "/portal/dashboard",
    )

    assert response.status_code == 401
    assert "use your customer number" in response.body.decode().lower()
    assert customer_portal.SESSION_COOKIE_NAME not in _response_cookies(response)


def test_existing_email_username_wrong_password_does_not_try_pppoe(
    db_session, subscriber, monkeypatch
):
    radius_attempted = False

    def _unexpected_radius_attempt(**_kwargs):
        nonlocal radius_attempted
        radius_attempted = True
        return None

    monkeypatch.setattr(
        web_customer_auth_service.radius_auth,
        "authenticate",
        _unexpected_radius_attempt,
    )
    subscriber.email = "existing-login@example.com"
    credential = UserCredential(
        subscriber_id=subscriber.id,
        provider=AuthProvider.local,
        username=subscriber.email,
        password_hash=hash_password("portal-secret"),
        is_active=True,
    )
    db_session.add(credential)
    db_session.commit()

    response = web_customer_auth_service.customer_login_submit(
        _request(),
        db_session,
        subscriber.email,
        "wrong-password",
        False,
        "/portal/dashboard",
    )

    db_session.refresh(credential)
    assert response.status_code == 401
    assert credential.failed_login_attempts == 1
    assert radius_attempted is False


def test_customer_login_records_local_failure_when_pppoe_fallback_fails(
    db_session, subscriber, monkeypatch
):
    monkeypatch.setattr(
        web_customer_auth_service.radius_auth,
        "authenticate",
        lambda **_kwargs: (_ for _ in ()).throw(RuntimeError("radius unavailable")),
    )
    local_credential = UserCredential(
        subscriber_id=subscriber.id,
        provider=AuthProvider.local,
        username="100024881",
        password_hash=hash_password("portal-secret"),
        is_active=True,
    )
    access_credential = AccessCredential(
        subscriber_id=subscriber.id,
        username="100024881",
        secret_hash="plain:pppoe-secret",
        is_active=True,
    )
    db_session.add_all([local_credential, access_credential])
    db_session.commit()

    response = web_customer_auth_service.customer_login_submit(
        _request(),
        db_session,
        "100024881",
        "wrong-secret",
        False,
        "/portal/dashboard",
    )

    db_session.refresh(local_credential)
    assert response.status_code == 401
    assert customer_portal.SESSION_COOKIE_NAME not in _response_cookies(response)
    assert local_credential.failed_login_attempts == 1


def test_customer_login_locked_account_shows_remaining_cooldown(
    db_session, subscriber, monkeypatch
):
    monkeypatch.setattr(
        web_customer_auth_service.radius_auth,
        "authenticate",
        lambda **_kwargs: (_ for _ in ()).throw(RuntimeError("radius unavailable")),
    )
    local_credential = UserCredential(
        subscriber_id=subscriber.id,
        provider=AuthProvider.local,
        username="100024882",
        password_hash=hash_password("portal-secret"),
        is_active=True,
        failed_login_attempts=5,
        locked_until=datetime.now(UTC) + timedelta(minutes=10),
    )
    db_session.add(local_credential)
    db_session.commit()

    response = web_customer_auth_service.customer_login_submit(
        _request(),
        db_session,
        "100024882",
        "portal-secret",
        False,
        "/portal/dashboard",
    )

    assert response.status_code == 401
    body = response.body.decode()
    assert "Account locked. Try again in 10 minutes." in body


def test_customer_login_radius_throttle_shows_remaining_cooldown(
    db_session, monkeypatch
):
    monkeypatch.setattr(
        web_customer_auth_service,
        "allow_operation",
        lambda *_args, **_kwargs: SimpleNamespace(
            allowed=False,
            retry_after_seconds=125,
        ),
    )

    response = web_customer_auth_service.customer_login_submit(
        _request(),
        db_session,
        "100024883",
        "portal-secret",
        False,
        "/portal/dashboard",
    )

    assert response.status_code == 401
    body = response.body.decode()
    assert "Account locked. Try again in 3 minutes." in body


def test_customer_login_redirects_to_mfa_when_enabled(
    db_session, subscriber, monkeypatch, portal_successes
):
    monkeypatch.setenv("JWT_SECRET", "test-secret")
    monkeypatch.setenv("TOTP_ENCRYPTION_KEY", Fernet.generate_key().decode("utf-8"))
    subscriber.email = "mfa-customer@example.com"
    credential = UserCredential(
        subscriber_id=subscriber.id,
        provider=AuthProvider.local,
        username="105000113",
        password_hash=hash_password("secret"),
        is_active=True,
    )
    db_session.add(credential)
    db_session.commit()

    setup = seed_enabled_subscriber_mfa(
        db_session, subscriber_id=subscriber.id, label="device"
    )

    response = web_customer_auth_service.customer_login_submit(
        _request(),
        db_session,
        "MFA-CUSTOMER@EXAMPLE.COM",
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
    assert portal_successes == []


def test_customer_mfa_submit_creates_customer_session(
    db_session, subscriber, monkeypatch, portal_successes
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

    setup = seed_enabled_subscriber_mfa(
        db_session, subscriber_id=subscriber.id, label="device"
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
    assert portal_successes == []

    valid_response = web_customer_auth_service.customer_mfa_submit(
        _request(cookies=pending_cookies),
        db_session,
        pyotp.TOTP(setup.secret).now(),
    )

    assert valid_response.status_code == 303
    assert portal_successes == ["user_credential"]
    assert valid_response.headers["location"] == "/portal/profile"
    cookies = _response_cookies(valid_response)
    assert customer_portal.SESSION_COOKIE_NAME in cookies
    assert customer_portal.get_customer_session(
        cookies[customer_portal.SESSION_COOKIE_NAME]
    )


def test_customer_forgot_password_submit_uses_shared_flow(monkeypatch, db_session):
    from unittest.mock import MagicMock

    recovery_request = MagicMock()
    monkeypatch.setattr(
        web_customer_auth_service.anonymous_recovery_adapter,
        "request_password_recovery",
        recovery_request,
    )

    response = web_customer_auth_service.customer_forgot_password_submit(
        _request(), db_session, "customer@example.com"
    )

    assert response.status_code == 200
    assert "Check your email" in response.body.decode()
    recovery_request.assert_called_once()
    command = recovery_request.call_args.kwargs["command"]
    assert command.email == "customer@example.com"
    assert command.next_login_path == "/portal/auth/login?next=/portal/dashboard"


def test_customer_forgot_password_page_renders_form(monkeypatch, db_session):
    monkeypatch.setattr(
        web_customer_auth_service,
        "get_current_customer_from_request",
        lambda request, db: None,
    )

    response = web_customer_auth_service.customer_forgot_password_page(
        _request(), db_session
    )

    assert response.status_code == 200
    body = response.body.decode()
    assert 'action="/portal/auth/forgot-password"' in body
    assert "_csrf_token" in body
    assert 'x-data="{ loading: false }"' in body
    assert 'x-on:submit="loading = true"' in body
    assert ':disabled="loading"' in body
    assert "loading ? 'Sending...' : 'Send reset link'" in body


def test_customer_forgot_password_page_redirects_signed_in(monkeypatch, db_session):
    monkeypatch.setattr(
        web_customer_auth_service,
        "get_current_customer_from_request",
        lambda request, db: {"subscriber_id": "x"},
    )

    response = web_customer_auth_service.customer_forgot_password_page(
        _request(), db_session
    )

    assert response.status_code == 303
    assert response.headers["location"] == "/portal/dashboard"


def _local_portal_credential(db_session, subscriber):
    credential = UserCredential(
        subscriber_id=subscriber.id,
        provider=AuthProvider.local,
        username=f"portal-standing-{subscriber.id}",
        password_hash=hash_password("portal-secret"),
        is_active=True,
    )
    db_session.add(credential)
    db_session.commit()
    return credential


def test_customer_login_commits_auth_standing_and_audit(db_session, subscriber):
    from sqlalchemy import select

    from app.models.audit import AuditEvent
    from app.models.auth import Session as AuthSession

    credential = _local_portal_credential(db_session, subscriber)
    response = web_customer_auth_service.customer_login_submit(
        _request(), db_session, credential.username, "portal-secret", False, None
    )
    assert response.status_code == 303
    token = _response_cookies(response)[customer_portal.SESSION_COOKIE_NAME]
    payload = customer_portal.get_customer_session(token)
    assert payload and payload["auth_session_id"]
    row = db_session.get(AuthSession, UUID(payload["auth_session_id"]))
    assert row and row.subscriber_id == subscriber.id
    expiry = row.expires_at.replace(tzinfo=UTC)
    assert expiry <= datetime.now(UTC) + timedelta(
        seconds=customer_portal.get_session_max_age(db_session)
    )
    audit = db_session.scalar(
        select(AuditEvent).where(AuditEvent.action == "auth.login_succeeded")
    )
    assert audit is not None
    assert customer_portal.get_current_customer(token, db_session) is not None


def test_customer_session_rejects_missing_revoked_expired_wrong_principal_standing(
    db_session,
    subscriber,
    monkeypatch,
):
    from app.models.auth import Session as AuthSession
    from app.models.auth import SessionStatus
    from app.services import customer_portal_session as sessions

    credential = _local_portal_credential(db_session, subscriber)
    response = web_customer_auth_service.customer_login_submit(
        _request(), db_session, credential.username, "portal-secret", False, None
    )
    token = _response_cookies(response)[customer_portal.SESSION_COOKIE_NAME]
    payload = dict(customer_portal.get_customer_session(token))
    row = db_session.get(AuthSession, UUID(payload["auth_session_id"]))
    monkeypatch.setattr(sessions, "get_customer_session", lambda _token: dict(payload))
    monkeypatch.setattr(sessions, "invalidate_customer_session", lambda _token: None)
    for changes in (
        {"auth_session_id": None},
        {"auth_session_id": "malformed"},
        {"subscriber_id": "00000000-0000-0000-0000-000000000001"},
        {"auth_session_id": None, "is_impersonation": True},
    ):
        original = dict(payload)
        payload.update(changes)
        assert customer_portal.get_current_customer(token, db_session) is None
        payload.clear()
        payload.update(original)
    row.status = SessionStatus.revoked
    db_session.commit()
    assert customer_portal.get_current_customer(token, db_session) is None
    row.status = SessionStatus.active
    row.revoked_at = datetime.now(UTC)
    db_session.commit()
    assert customer_portal.get_current_customer(token, db_session) is None
    row.revoked_at = None
    row.expires_at = datetime.now(UTC) - timedelta(seconds=1)
    db_session.commit()
    assert customer_portal.get_current_customer(token, db_session) is None


@pytest.mark.parametrize("failure", [RuntimeError, ValueError])
def test_customer_redis_failure_revokes_committed_auth_session(
    db_session, subscriber, monkeypatch, failure, portal_successes
):
    from sqlalchemy import select

    from app.models.auth import Session as AuthSession
    from app.models.auth import SessionStatus

    credential = _local_portal_credential(db_session, subscriber)

    def fail(**_kwargs):
        raise failure("Session store unavailable")

    monkeypatch.setattr(customer_portal, "create_customer_session", fail)
    response = web_customer_auth_service.customer_login_submit(
        _request(), db_session, credential.username, "portal-secret", False, None
    )
    assert response.status_code == 503
    assert portal_successes == []
    assert customer_portal.SESSION_COOKIE_NAME not in _response_cookies(response)
    row = db_session.scalar(
        select(AuthSession).where(AuthSession.subscriber_id == subscriber.id)
    )
    assert row and row.status == SessionStatus.revoked and row.revoked_at


def test_customer_audit_failure_rolls_back_gate_and_session(
    db_session, subscriber, monkeypatch, portal_successes
):
    from sqlalchemy import func, select

    from app.models.auth import Session as AuthSession
    from app.services import password_authentication as pa

    credential = _local_portal_credential(db_session, subscriber)

    def fail(*_args, **_kwargs):
        raise RuntimeError("audit unavailable")

    monkeypatch.setattr(pa, "_stage_audit", fail)
    response = web_customer_auth_service.customer_login_submit(
        _request(), db_session, credential.username, "portal-secret", False, None
    )
    assert response.status_code == 503
    assert portal_successes == []
    db_session.refresh(credential)
    assert credential.last_login_at is None
    assert db_session.scalar(select(func.count()).select_from(AuthSession)) == 0


def test_customer_mfa_pending_has_no_session_or_login_success(
    db_session, subscriber, monkeypatch
):
    monkeypatch.setenv("TOTP_ENCRYPTION_KEY", Fernet.generate_key().decode())
    from sqlalchemy import func, select

    from app.models.audit import AuditEvent
    from app.models.auth import Session as AuthSession
    from app.services import password_authentication as pa

    credential = _local_portal_credential(db_session, subscriber)
    setup = seed_enabled_subscriber_mfa(
        db_session, subscriber_id=subscriber.id, label="device"
    )
    response = web_customer_auth_service.customer_login_submit(
        _request(), db_session, credential.username, "portal-secret", False, None
    )
    cookies = _response_cookies(response)
    challenge = pa.decode_challenge(
        db_session, cookies["customer_mfa_pending"], expected_typ="mfa"
    )
    assert (
        challenge.credential_id == credential.id and challenge.credential_version == 1
    )
    assert customer_portal.SESSION_COOKIE_NAME not in cookies
    assert db_session.scalar(select(func.count()).select_from(AuthSession)) == 0
    assert (
        db_session.scalar(
            select(func.count())
            .select_from(AuditEvent)
            .where(AuditEvent.action == "auth.login_succeeded")
        )
        == 0
    )
    assert (
        db_session.scalar(
            select(func.count())
            .select_from(AuditEvent)
            .where(AuditEvent.action == "auth.password_step_succeeded")
        )
        == 1
    )
    db_session.refresh(credential)
    assert credential.last_login_at is None


def test_customer_mfa_rejects_reset_version_without_consuming_method(
    db_session, subscriber, monkeypatch
):
    monkeypatch.setenv("TOTP_ENCRYPTION_KEY", Fernet.generate_key().decode())
    from app.models.auth import MFAMethod

    credential = _local_portal_credential(db_session, subscriber)
    setup = seed_enabled_subscriber_mfa(
        db_session, subscriber_id=subscriber.id, label="device"
    )
    response = web_customer_auth_service.customer_login_submit(
        _request(), db_session, credential.username, "portal-secret", False, None
    )
    cookies = _response_cookies(response)
    credential.credential_version += 1
    db_session.commit()
    method = db_session.get(MFAMethod, setup.method_id)
    before = method.last_used_at
    result = web_customer_auth_service.customer_mfa_submit(
        _request(cookies), db_session, pyotp.TOTP(setup.secret).now()
    )
    assert result.status_code == 401
    db_session.refresh(method)
    assert method.last_used_at == before


@pytest.mark.parametrize("marker", ["radius", "access"])
def test_radius_customer_mfa_preserves_source_and_rejects_marker_change(
    db_session,
    subscriber,
    monkeypatch,
    marker,
    portal_successes,
):
    from app.models.radius import RadiusUser
    from app.services import password_authentication as pa

    monkeypatch.setenv("TOTP_ENCRYPTION_KEY", Fernet.generate_key().decode())
    monkeypatch.setattr(
        web_customer_auth_service.radius_auth, "authenticate", lambda **_kwargs: None
    )
    access = AccessCredential(
        subscriber_id=subscriber.id,
        username="radius-standing",
        secret_hash="plain:radius-secret",
        is_active=True,
    )
    db_session.add(access)
    db_session.flush()
    radius = RadiusUser(
        subscriber_id=subscriber.id,
        access_credential_id=access.id,
        username=access.username,
        secret_hash=access.secret_hash,
        is_active=True,
    )
    db_session.add(radius)
    db_session.commit()
    setup = seed_enabled_subscriber_mfa(
        db_session, subscriber_id=subscriber.id, label="device"
    )
    response = web_customer_auth_service.customer_login_submit(
        _request(), db_session, access.username, "radius-secret", False, None
    )
    assert response.status_code == 303
    cookies = _response_cookies(response)
    challenge = pa.decode_challenge(
        db_session, cookies["customer_mfa_pending"], expected_typ="mfa"
    )
    assert challenge.src == pa.SRC_RADIUS_USER
    assert (
        challenge.radius_user_id == radius.id
        and challenge.access_credential_id == access.id
    )
    changed = radius if marker == "radius" else access
    changed.updated_at = datetime.now(UTC) + timedelta(seconds=1)
    db_session.commit()
    denied = web_customer_auth_service.customer_mfa_submit(
        _request(cookies), db_session, pyotp.TOTP(setup.secret).now()
    )
    assert denied.status_code == 401
    assert portal_successes == []
    # A fresh radius-backed password step and unchanged markers still complete.
    response = web_customer_auth_service.customer_login_submit(
        _request(), db_session, access.username, "radius-secret", False, None
    )
    cookies = _response_cookies(response)
    allowed = web_customer_auth_service.customer_mfa_submit(
        _request(cookies), db_session, pyotp.TOTP(setup.secret).now()
    )
    assert allowed.status_code == 303
    assert portal_successes == ["radius_user"]
    token = _response_cookies(allowed)[customer_portal.SESSION_COOKIE_NAME]
    assert customer_portal.get_current_customer(token, db_session) is not None


def test_pppoe_customer_login_leaves_local_counters_unchanged(
    db_session, subscriber, monkeypatch
):
    monkeypatch.setattr(
        web_customer_auth_service.radius_auth,
        "authenticate",
        lambda **_kwargs: (_ for _ in ()).throw(RuntimeError("unavailable")),
    )
    local = UserCredential(
        subscriber_id=subscriber.id,
        provider=AuthProvider.local,
        username="pppoe-standing",
        password_hash=hash_password("local-secret"),
        failed_login_attempts=2,
        is_active=True,
    )
    access = AccessCredential(
        subscriber_id=subscriber.id,
        username=local.username,
        secret_hash="plain:pppoe-secret",
        is_active=True,
    )
    db_session.add_all([local, access])
    db_session.commit()
    before = (
        local.failed_login_attempts,
        local.locked_until,
        local.last_login_at,
        local.credential_version,
    )
    response = web_customer_auth_service.customer_login_submit(
        _request(), db_session, local.username, "pppoe-secret", False, None
    )
    assert response.status_code == 303
    db_session.refresh(local)
    assert (
        local.failed_login_attempts,
        local.locked_until,
        local.last_login_at,
        local.credential_version,
    ) == before
    token = _response_cookies(response)[customer_portal.SESSION_COOKIE_NAME]
    assert customer_portal.get_current_customer(token, db_session) is not None


@pytest.mark.parametrize(
    "submitted,expected,representation",
    [
        ("access-secret", 303, "plain"),
        ("radius-only-secret", 401, "plain"),
        ("access-secret", 401, "bcrypt"),
    ],
)
def test_radius_without_projection_rechecks_pre_network_access_secret(
    db_session,
    subscriber,
    monkeypatch,
    submitted,
    expected,
    representation,
):
    import bcrypt

    monkeypatch.setattr(
        web_customer_auth_service.radius_auth, "authenticate", lambda **_kwargs: None
    )
    access = AccessCredential(
        subscriber_id=subscriber.id,
        username="radius-no-projection",
        secret_hash=(
            bcrypt.hashpw(b"access-secret", bcrypt.gensalt(rounds=4)).decode()
            if representation == "bcrypt"
            else "plain:access-secret"
        ),
        is_active=True,
    )
    db_session.add(access)
    db_session.commit()
    response = web_customer_auth_service.customer_login_submit(
        _request(), db_session, access.username, submitted, False, None
    )
    assert response.status_code == expected


def test_customer_login_lock_timeout_sets_retry_after(
    db_session, subscriber, monkeypatch
):
    from app.services import password_authentication as pa

    credential = _local_portal_credential(db_session, subscriber)

    def fail(*_args, **_kwargs):
        raise pa.PasswordAuthenticationError("lock_timeout", "Try again")

    monkeypatch.setattr(pa, "complete_password_step", fail)
    response = web_customer_auth_service.customer_login_submit(
        _request(), db_session, credential.username, "portal-secret", False, None
    )
    assert response.status_code == 503
    assert response.headers["retry-after"] == "5"
    assert customer_portal.SESSION_COOKIE_NAME not in _response_cookies(response)


def test_customer_mfa_lock_timeout_sets_retry_after(db_session, monkeypatch):
    from app.services import password_authentication as pa

    monkeypatch.setattr(
        web_customer_auth_service,
        "_decode_customer_mfa_context",
        lambda *_args, **_kwargs: {},
    )

    def fail(*_args, **_kwargs):
        raise pa.PasswordAuthenticationError("lock_timeout", "Try again")

    monkeypatch.setattr(web_customer_auth_service, "_verify_customer_mfa_token", fail)
    response = web_customer_auth_service.customer_mfa_submit(
        _request(
            {"customer_mfa_pending": "pending", "customer_mfa_context": "context"}
        ),
        db_session,
        "123456",
    )
    assert response.status_code == 503 and response.headers["retry-after"] == "5"
    assert customer_portal.SESSION_COOKIE_NAME not in _response_cookies(response)


@pytest.mark.parametrize(
    "refusal", ["legacy", "expired", "context_principal", "audience"]
)
def test_customer_mfa_rejects_unbound_expired_or_mismatched_challenge(
    db_session,
    subscriber,
    monkeypatch,
    refusal,
):
    from uuid import uuid4

    from sqlalchemy import func, select

    from app.models.auth import MFAMethod
    from app.models.auth import Session as AuthSession
    from app.services import auth_flow as af

    monkeypatch.setenv("TOTP_ENCRYPTION_KEY", Fernet.generate_key().decode())
    credential = _local_portal_credential(db_session, subscriber)
    setup = seed_enabled_subscriber_mfa(
        db_session, subscriber_id=subscriber.id, label="device"
    )
    response = web_customer_auth_service.customer_login_submit(
        _request(), db_session, credential.username, "portal-secret", False, None
    )
    cookies = _response_cookies(response)
    method = db_session.get(MFAMethod, setup.method_id)
    before = (method.last_used_at, method.failed_attempts)
    key = (
        "customer_mfa_context"
        if refusal == "context_principal"
        else "customer_mfa_pending"
    )
    payload = af._jwt_decode_token(
        cookies[key], af._jwt_secret(db_session), af._jwt_algorithm(db_session)
    )
    if refusal == "legacy":
        payload["ver"] = 1
    elif refusal == "expired":
        payload["exp"] = int((datetime.now(UTC) - timedelta(seconds=30)).timestamp())
    elif refusal == "context_principal":
        payload["subscriber_id"] = payload["account_id"] = str(uuid4())
    else:
        payload["login_aud"] = "admin"
    cookies[key] = af._jwt_encode_token(
        payload, af._jwt_secret(db_session), af._jwt_algorithm(db_session)
    )
    denied = web_customer_auth_service.customer_mfa_submit(
        _request(cookies), db_session, pyotp.TOTP(setup.secret).now()
    )
    assert denied.status_code == 401
    assert customer_portal.SESSION_COOKIE_NAME not in _response_cookies(denied)
    db_session.refresh(method)
    assert (method.last_used_at, method.failed_attempts) == before
    assert db_session.scalar(select(func.count()).select_from(AuthSession)) == 0
