"""The vendor JSON authentication API answers the contract the field app calls.

``field_mobile`` posts vendor credentials to ``/api/v1/vendor/auth/login``,
verifies MFA at ``/api/v1/vendor/auth/mfa`` and rotates tokens at
``/api/v1/vendor/auth/refresh``. Sub mounted none of them: its only
``/vendor/auth/login`` was the Jinja HTML route mounted with NO ``/api/v1``
prefix (``app/web/vendor_auth.py``), so every one of those calls answered 404
and vendor technicians could not sign in at all.

Two kinds of test live here:

* the CONTRACT PIN (``test_the_paths_the_field_app_calls_are_mounted`` and
  ``test_the_client_source_references_no_unmounted_api_path``) — the real
  defect class. Both sides were individually coherent; nothing compared them.
  The first pins the literal path strings so the pin survives even if the Dart
  tree moves; the second reads the shipped client and fails the build when it
  starts calling something Sub does not serve.
* the BEHAVIOUR tests — each restored endpoint returns the body the Dart client
  actually parses, and vendor admission is enforced on every token it hands out.

Sub is a single-operator-tenant deployment (ADR-0009), so the boundary a vendor
principal must not cross is its VENDOR membership and its principal type: a
vendor token is a staff (``system_user``) token admitted to exactly one
``FieldVendor``, never a subscriber-scoped one, and never another vendor's.
"""

from __future__ import annotations

import ast
import re
from http.cookies import SimpleCookie
from pathlib import Path
from uuid import uuid4

import pyotp
import pytest
from cryptography.fernet import Fernet
from fastapi import FastAPI
from fastapi.testclient import TestClient
from starlette.requests import Request

from app.api import vendor_auth as vendor_auth_api
from app.db import get_db
from app.main import _DEFERRED_API_ROUTER_SPECS
from app.models.auth import AuthProvider, SessionStatus, UserCredential
from app.models.auth import Session as AuthSession
from app.models.field_vendor import FieldVendor, FieldVendorUser
from app.models.subscriber import UserType
from app.models.system_user import SystemUser
from app.models.vendor_routes import Vendor
from app.services import auth_flow as auth_flow_service
from app.services.auth_flow import AuthFlow, decode_access_token, hash_password
from tests.staff_identity_fixtures import project_staff_login

PROJECT_ROOT = Path(__file__).resolve().parents[1]

# The exact strings the shipped field app posts to. Pinned as literals, not
# derived: a build already in technicians' hands cannot be redeployed atomically
# with the server, so these paths are frozen until that build is retired.
CLIENT_VENDOR_AUTH_PATHS = (
    "/api/v1/vendor/auth/login",
    "/api/v1/vendor/auth/mfa",
    "/api/v1/vendor/auth/refresh",
)

# Dart sources that own the client half of this contract.
CLIENT_SOURCES = (
    PROJECT_ROOT / "field_mobile/lib/features/auth/auth_repository.dart",
    PROJECT_ROOT / "field_mobile/lib/core/api/api_client.dart",
)

NATIVE_REFRESH_HEADERS = {"X-Auth-Refresh-In-Body": "true"}


# ── fixtures ────────────────────────────────────────────────────────────────


def _vendor_identity(db_session, *, username: str) -> tuple[SystemUser, FieldVendor]:
    system_user = SystemUser(
        first_name="Vendor",
        last_name="Technician",
        display_name="Vendor Technician",
        email=username,
        user_type=UserType.vendor,
        is_active=True,
    )
    native_vendor = Vendor(name=f"Vendor {uuid4().hex[:8]}")
    db_session.add_all([system_user, native_vendor])
    db_session.flush()
    field_vendor = FieldVendor(
        name=native_vendor.name,
        code=f"VA-{uuid4().hex[:8]}",
        crm_vendor_id=str(native_vendor.id),
        is_active=True,
    )
    credential = UserCredential(
        system_user_id=system_user.id,
        provider=AuthProvider.local,
        username=username,
        password_hash=hash_password("secret-123"),
        is_active=True,
        must_change_password=False,
    )
    db_session.add(field_vendor)
    project_staff_login(db_session, user=system_user, credential=credential)
    db_session.add(
        FieldVendorUser(
            vendor_id=field_vendor.id,
            system_user_id=system_user.id,
            role="owner",
            is_active=True,
        )
    )
    db_session.commit()
    return system_user, field_vendor


def _non_vendor_staff(db_session, *, username: str) -> SystemUser:
    system_user = SystemUser(
        first_name="Staff",
        last_name="Only",
        email=username,
        user_type=UserType.system_user,
        is_active=True,
    )
    db_session.add(system_user)
    db_session.flush()
    credential = UserCredential(
        system_user_id=system_user.id,
        provider=AuthProvider.local,
        username=username,
        password_hash=hash_password("secret-123"),
        is_active=True,
    )
    project_staff_login(db_session, user=system_user, credential=credential)
    db_session.commit()
    return system_user


def _subscriber_credential(db_session, subscriber, *, username: str) -> UserCredential:
    credential = UserCredential(
        subscriber_id=subscriber.id,
        provider=AuthProvider.local,
        username=username,
        password_hash=hash_password("secret-123"),
        is_active=True,
    )
    db_session.add(credential)
    db_session.commit()
    return credential


def _enable_mfa(db_session, system_user: SystemUser, monkeypatch) -> pyotp.TOTP:
    monkeypatch.setenv("TOTP_ENCRYPTION_KEY", Fernet.generate_key().decode())
    setup = AuthFlow.admin_mfa_setup(
        db_session, str(system_user.id), label="Vendor API test authenticator"
    )
    totp = pyotp.TOTP(setup["secret"])
    AuthFlow.admin_mfa_confirm(
        db_session, str(setup["method_id"]), totp.now(), str(system_user.id)
    )
    return totp


def _client(db_session) -> TestClient:
    app = FastAPI()
    app.include_router(vendor_auth_api.router, prefix="/api/v1")
    app.dependency_overrides[get_db] = lambda: db_session
    return TestClient(app, raise_server_exceptions=False)


def _raw_request(path: str = "/api/v1/vendor/auth/login") -> Request:
    """A minimal Request for calling the auth owner directly (no route)."""

    return Request(
        {
            "type": "http",
            "method": "POST",
            "scheme": "http",
            "server": ("testserver", 80),
            "client": ("127.0.0.1", 12345),
            "path": path,
            "query_string": b"",
            "headers": [(b"host", b"testserver"), (b"user-agent", b"vendor-api-test")],
        }
    )


def _sessions_for(db_session, system_user: SystemUser) -> list[AuthSession]:
    return (
        db_session.query(AuthSession)
        .filter(AuthSession.system_user_id == system_user.id)
        .all()
    )


# ── the contract pin ────────────────────────────────────────────────────────


def _mounted_api_paths() -> set[str]:
    from tests.architecture import openapi_contract_lib as lib

    return {
        path
        for path in lib.build_full_app().openapi().get("paths", {})
        if path.startswith("/api/v1")
    }


@pytest.mark.parametrize("path", CLIENT_VENDOR_AUTH_PATHS)
def test_the_paths_the_field_app_calls_are_mounted(path: str) -> None:
    """The whole outage in one assertion: these three answered 404."""

    assert path in _mounted_api_paths(), (
        f"{path} is not mounted. The field app posts vendor credentials there; "
        "an unmounted path is a total vendor-login outage, not a degraded "
        "feature. Restore the route — do not point it at the Jinja handler in "
        "app/web/vendor_auth.py, which is the browser transport."
    )


def test_the_client_source_references_no_unmounted_api_path() -> None:
    """Read the shipped client and compare it with what Sub serves.

    This is the guard the defect needed: both halves were coherent on their own
    and nothing ever compared them.
    """

    present = [path for path in CLIENT_SOURCES if path.exists()]
    if not present:
        pytest.skip("field_mobile client sources are not in this checkout")

    mounted = _mounted_api_paths()
    referenced: set[str] = set()
    for source in present:
        referenced.update(re.findall(r"'(/api/v1/[^'{$]*)'", source.read_text()))

    missing = sorted(path for path in referenced if path not in mounted)
    assert not missing, (
        "the field app calls /api/v1 paths Sub does not mount; each is a 404 "
        f"in production: {missing}"
    )
    assert referenced, "no /api/v1 paths were extracted — the scan stopped working"


def test_the_vendor_auth_api_is_mounted_pre_auth_and_before_the_vendor_portal() -> None:
    auth_spec = ("app.api.vendor_auth", "router", "api", "none")
    portal_spec = ("app.api.vendor_portal", "router", "api", "user")

    assert auth_spec in _DEFERRED_API_ROUTER_SPECS, (
        "the vendor auth API must mount with dependency mode 'none' — it IS "
        "the authentication, so it cannot require an authenticated principal"
    )
    assert _DEFERRED_API_ROUTER_SPECS.index(auth_spec) < (
        _DEFERRED_API_ROUTER_SPECS.index(portal_spec)
    ), "the authenticated /vendor surface must never shadow /vendor/auth"


def test_the_html_vendor_login_is_untouched_and_still_separately_mounted() -> None:
    """The fix adds a transport; it does not alias or replace the browser one."""

    assert (
        "app.web.vendor_auth",
        "router",
        "web",
        "none",
    ) in _DEFERRED_API_ROUTER_SPECS
    source = (PROJECT_ROOT / "app/api/vendor_auth.py").read_text()
    assert "app.web" not in source, (
        "the JSON adapter must not reach into the browser adapter. Both are "
        "thin adapters over the same owners; neither wraps the other."
    )


# ── behaviour: the restored endpoints ───────────────────────────────────────


def test_vendor_login_returns_the_token_shape_the_client_parses(db_session) -> None:
    username = f"vendor-{uuid4().hex[:8]}@example.com"
    _system_user, field_vendor = _vendor_identity(db_session, username=username)

    response = _client(db_session).post(
        "/api/v1/vendor/auth/login",
        json={"username": username, "password": "secret-123"},
        headers=NATIVE_REFRESH_HEADERS,
    )

    assert response.status_code == 200, response.text
    body = response.json()
    # Exactly the keys AuthRepository._handleTokens reads.
    assert body["access_token"]
    assert body["refresh_token"]
    assert body["vendor_id"] == str(field_vendor.id)
    assert body["mfa_required"] is False


def test_vendor_login_rejects_a_wrong_password_with_the_shared_401(db_session) -> None:
    username = f"vendor-{uuid4().hex[:8]}@example.com"
    _vendor_identity(db_session, username=username)

    response = _client(db_session).post(
        "/api/v1/vendor/auth/login",
        json={"username": username, "password": "wrong-password"},
        headers=NATIVE_REFRESH_HEADERS,
    )

    assert response.status_code == 401
    assert isinstance(response.json()["detail"], str)


def test_a_browser_caller_keeps_the_refresh_token_in_an_httponly_cookie(
    db_session,
) -> None:
    """Transport policy is the auth owner's, and this adapter applies it."""

    username = f"vendor-{uuid4().hex[:8]}@example.com"
    _vendor_identity(db_session, username=username)

    response = _client(db_session).post(
        "/api/v1/vendor/auth/login",
        json={"username": username, "password": "secret-123"},
    )

    assert response.status_code == 200, response.text
    assert response.json()["refresh_token"] is None
    jar = SimpleCookie()
    for header, value in response.headers.multi_items():
        if header.lower() == "set-cookie":
            jar.load(value)
    settings = AuthFlow.refresh_cookie_settings(db_session)
    assert settings["key"] in jar
    assert jar[settings["key"]]["httponly"]


def test_vendor_login_challenges_with_the_mfa_shape_the_client_parses(
    db_session, monkeypatch
) -> None:
    username = f"vendor-mfa-{uuid4().hex[:8]}@example.com"
    system_user, _vendor = _vendor_identity(db_session, username=username)
    _enable_mfa(db_session, system_user, monkeypatch)

    response = _client(db_session).post(
        "/api/v1/vendor/auth/login",
        json={"username": username, "password": "secret-123"},
        headers=NATIVE_REFRESH_HEADERS,
    )

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["mfa_required"] is True
    assert body["mfa_token"]
    assert body["access_token"] is None


def test_vendor_mfa_verification_completes_the_login(db_session, monkeypatch) -> None:
    username = f"vendor-mfa-{uuid4().hex[:8]}@example.com"
    system_user, field_vendor = _vendor_identity(db_session, username=username)
    totp = _enable_mfa(db_session, system_user, monkeypatch)
    client = _client(db_session)

    challenge = client.post(
        "/api/v1/vendor/auth/login",
        json={"username": username, "password": "secret-123"},
        headers=NATIVE_REFRESH_HEADERS,
    ).json()

    response = client.post(
        "/api/v1/vendor/auth/mfa",
        json={"mfa_token": challenge["mfa_token"], "code": totp.now()},
        headers=NATIVE_REFRESH_HEADERS,
    )

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["access_token"]
    assert body["refresh_token"]
    assert body["vendor_id"] == str(field_vendor.id)


def test_vendor_mfa_rejects_an_invalid_code(db_session, monkeypatch) -> None:
    username = f"vendor-mfa-{uuid4().hex[:8]}@example.com"
    system_user, _vendor = _vendor_identity(db_session, username=username)
    totp = _enable_mfa(db_session, system_user, monkeypatch)
    client = _client(db_session)
    challenge = client.post(
        "/api/v1/vendor/auth/login",
        json={"username": username, "password": "secret-123"},
        headers=NATIVE_REFRESH_HEADERS,
    ).json()
    wrong = "000000" if totp.now() != "000000" else "111111"

    response = client.post(
        "/api/v1/vendor/auth/mfa",
        json={"mfa_token": challenge["mfa_token"], "code": wrong},
        headers=NATIVE_REFRESH_HEADERS,
    )

    assert response.status_code == 401


def test_vendor_refresh_rotates_the_pair(db_session) -> None:
    username = f"vendor-{uuid4().hex[:8]}@example.com"
    _system_user, field_vendor = _vendor_identity(db_session, username=username)
    client = _client(db_session)
    login = client.post(
        "/api/v1/vendor/auth/login",
        json={"username": username, "password": "secret-123"},
        headers=NATIVE_REFRESH_HEADERS,
    ).json()

    response = client.post(
        "/api/v1/vendor/auth/refresh",
        json={"refresh_token": login["refresh_token"]},
        headers=NATIVE_REFRESH_HEADERS,
    )

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["access_token"]
    assert body["refresh_token"] != login["refresh_token"], "refresh must rotate"
    assert body["vendor_id"] == str(field_vendor.id)


def test_vendor_refresh_without_a_token_is_401_not_500(db_session) -> None:
    response = _client(db_session).post(
        "/api/v1/vendor/auth/refresh", json={}, headers=NATIVE_REFRESH_HEADERS
    )

    assert response.status_code == 401


def test_vendor_logout_revokes_the_session(db_session) -> None:
    username = f"vendor-{uuid4().hex[:8]}@example.com"
    system_user, _vendor = _vendor_identity(db_session, username=username)
    client = _client(db_session)
    login = client.post(
        "/api/v1/vendor/auth/login",
        json={"username": username, "password": "secret-123"},
        headers=NATIVE_REFRESH_HEADERS,
    ).json()

    response = client.post(
        "/api/v1/vendor/auth/logout",
        json={"refresh_token": login["refresh_token"]},
        headers=NATIVE_REFRESH_HEADERS,
    )

    assert response.status_code == 200, response.text
    assert response.json()["revoked_at"]
    assert [session.status for session in _sessions_for(db_session, system_user)] == [
        SessionStatus.revoked
    ]


# ── behaviour: the boundary a vendor token must not cross ───────────────────


def test_a_vendor_token_is_a_staff_principal_scoped_to_its_own_vendor(
    db_session,
) -> None:
    """Not subscriber-scoped, and carrying exactly one vendor's id."""

    username = f"vendor-{uuid4().hex[:8]}@example.com"
    system_user, field_vendor = _vendor_identity(db_session, username=username)
    other_vendor = FieldVendor(
        name="Other Co", code=f"VB-{uuid4().hex[:8]}", is_active=True
    )
    db_session.add(other_vendor)
    db_session.commit()

    body = (
        _client(db_session)
        .post(
            "/api/v1/vendor/auth/login",
            json={"username": username, "password": "secret-123"},
            headers=NATIVE_REFRESH_HEADERS,
        )
        .json()
    )

    payload = decode_access_token(db_session, body["access_token"])
    assert payload["principal_type"] == "system_user", (
        "a vendor login must not mint a subscriber-scoped principal"
    )
    assert payload["principal_id"] == str(system_user.id)
    assert body["vendor_id"] == str(field_vendor.id)
    assert body["vendor_id"] != str(other_vendor.id)


def test_non_vendor_staff_is_refused_before_the_password_is_verified(
    db_session, monkeypatch
) -> None:
    username = f"staff-{uuid4().hex[:8]}@example.com"
    _non_vendor_staff(db_session, username=username)
    calls: list[object] = []

    def _must_not_run(**kwargs: object) -> dict[str, object]:
        calls.append(kwargs)
        return {}

    monkeypatch.setattr(auth_flow_service.auth_flow, "login", _must_not_run)

    response = _client(db_session).post(
        "/api/v1/vendor/auth/login",
        json={"username": username, "password": "secret-123"},
        headers=NATIVE_REFRESH_HEADERS,
    )

    assert response.status_code == 403
    assert not calls, (
        "vendor admission must be decided before credentials are verified, so "
        "this endpoint never becomes a credential oracle for the staff surface"
    )


def test_a_subscriber_credential_cannot_obtain_a_vendor_token(
    db_session, subscriber
) -> None:
    username = f"customer-{uuid4().hex[:8]}@example.com"
    _subscriber_credential(db_session, subscriber, username=username)

    response = _client(db_session).post(
        "/api/v1/vendor/auth/login",
        json={"username": username, "password": "secret-123"},
        headers=NATIVE_REFRESH_HEADERS,
    )

    assert response.status_code == 403
    assert "access_token" not in response.json()


def test_a_staff_mfa_token_cannot_be_redeemed_on_the_vendor_endpoint(
    db_session, monkeypatch
) -> None:
    """The cross-surface boundary: a VALID mfa token from the staff login is
    still refused here, and the session it minted is discarded rather than left
    live for a principal who never received it."""

    username = f"staff-mfa-{uuid4().hex[:8]}@example.com"
    system_user = _non_vendor_staff(db_session, username=username)
    totp = _enable_mfa(db_session, system_user, monkeypatch)
    challenge = auth_flow_service.auth_flow.login(
        db=db_session,
        username=username,
        password="secret-123",
        request=_raw_request(),
        provider=None,
    )
    assert challenge["mfa_required"] is True

    response = _client(db_session).post(
        "/api/v1/vendor/auth/mfa",
        json={"mfa_token": challenge["mfa_token"], "code": totp.now()},
        headers=NATIVE_REFRESH_HEADERS,
    )

    assert response.status_code == 403
    assert "access_token" not in response.json()
    assert all(
        session.status is SessionStatus.revoked
        for session in _sessions_for(db_session, system_user)
    ), "a refused admission must not leave a live session behind"


def test_refresh_stops_admitting_a_deactivated_vendor_membership(db_session) -> None:
    """Membership revocation takes effect at the next rotation, not at expiry."""

    username = f"vendor-{uuid4().hex[:8]}@example.com"
    system_user, _vendor = _vendor_identity(db_session, username=username)
    client = _client(db_session)
    login = client.post(
        "/api/v1/vendor/auth/login",
        json={"username": username, "password": "secret-123"},
        headers=NATIVE_REFRESH_HEADERS,
    ).json()
    membership = (
        db_session.query(FieldVendorUser)
        .filter(FieldVendorUser.system_user_id == system_user.id)
        .one()
    )
    membership.is_active = False
    db_session.commit()

    response = client.post(
        "/api/v1/vendor/auth/refresh",
        json={"refresh_token": login["refresh_token"]},
        headers=NATIVE_REFRESH_HEADERS,
    )

    assert response.status_code == 403
    assert all(
        session.status is SessionStatus.revoked
        for session in _sessions_for(db_session, system_user)
    )


# ── the module stays an adapter ─────────────────────────────────────────────


def test_the_route_module_stays_a_thin_adapter() -> None:
    """No data access and no transaction control in the route module.

    ``tests/architecture/test_thin_wrappers.py`` sweeps every adapter for the
    query patterns; this adds the two rules that matter for THIS module — it
    must not own a transaction, and every route must delegate to one of the two
    named owners rather than growing its own copy of an auth decision.
    """

    source = (PROJECT_ROOT / "app/api/vendor_auth.py").read_text()
    for forbidden in ("db.query(", "db.execute(", "select(", "db.commit(", "db.add("):
        assert forbidden not in source, (
            f"{forbidden} in app/api/vendor_auth.py — logic and transactions "
            "belong to app/services/auth_flow.py and "
            "app/services/field/vendor_auth.py"
        )

    tree = ast.parse(source)
    owners = {"auth_flow_service", "auth_flow", "vendor_admission", "AuthFlow"}
    routes = [
        node
        for node in tree.body
        if isinstance(node, ast.FunctionDef)
        and any(
            isinstance(decorator, ast.Call)
            and isinstance(decorator.func, ast.Attribute)
            and isinstance(decorator.func.value, ast.Name)
            and decorator.func.value.id == "router"
            for decorator in node.decorator_list
        )
    ]
    assert {route.name for route in routes} == {
        "vendor_login",
        "vendor_mfa_verify",
        "vendor_refresh",
        "vendor_logout",
    }
    for route in routes:
        called_owners = {
            ast.unparse(node.func).split(".")[0]
            for node in ast.walk(route)
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
        }
        assert called_owners & owners, (
            f"{route.name} calls no owning service — a route that decides for "
            "itself is the parallel authority this module exists to avoid"
        )
