"""Layer 3 — reseller portal login as a first-class ResellerUser principal.

Verifies the dual-read auth foundation: a reseller can authenticate as a
ResellerUser (no backing Subscriber) end-to-end through auth_flow and the
reseller portal session — but only when the RESELLER_USER_PRINCIPAL_ENABLED flag
is on. With the flag off (default/prod), the path is inert and the existing
subscriber-backed reseller login is unchanged.
"""

from __future__ import annotations

import pytest
from fastapi import HTTPException
from starlette.requests import Request

from app.config import settings
from app.models.auth import AuthProvider, MFAMethod, MFAMethodType, UserCredential
from app.models.subscriber import Reseller
from app.services import auth_flow as auth_flow_service
from app.services import reseller_portal
from app.services.auth_flow import AuthFlow, _primary_totp_method


def _request():
    return Request(
        {
            "type": "http",
            "method": "POST",
            "path": "/auth",
            "headers": [(b"user-agent", b"pytest")],
            "client": ("127.0.0.1", 5555),
        }
    )


@pytest.fixture()
def reseller(db_session):
    r = Reseller(name="ABC Networks", code="ABC")
    db_session.add(r)
    db_session.commit()
    db_session.refresh(r)
    return r


@pytest.fixture()
def flag_on(monkeypatch):
    # settings is a frozen dataclass; flip it the way the app does elsewhere.
    monkeypatch.setenv("JWT_SECRET", "test-secret")
    old = settings.reseller_user_principal_enabled
    object.__setattr__(settings, "reseller_user_principal_enabled", True)
    yield
    object.__setattr__(settings, "reseller_user_principal_enabled", old)


def _make_reseller_login(db, reseller, username="abc-admin", password="secret"):  # noqa: S107
    return reseller_portal.create_reseller_user_principal(
        db,
        reseller_id=str(reseller.id),
        username=username,
        password=password,
        email="owner@abcnetworks.com",
        full_name="ABC Owner",
    )


def test_create_reseller_user_principal_has_no_subscriber(db_session, reseller):
    ru = _make_reseller_login(db_session, reseller)
    cred = (
        db_session.query(UserCredential)
        .filter(UserCredential.reseller_user_id == ru.id)
        .one()
    )
    assert cred.subscriber_id is None
    assert cred.system_user_id is None
    assert cred.reseller_user_id == ru.id
    assert ru.email == "owner@abcnetworks.com"


def test_login_issues_reseller_user_principal_token(db_session, reseller, flag_on):
    _make_reseller_login(db_session, reseller)
    tokens = AuthFlow.login(db_session, "abc-admin", "secret", _request(), None)
    payload = auth_flow_service.decode_access_token(db_session, tokens["access_token"])
    assert payload["principal_type"] == "reseller_user"


def test_login_blocked_when_flag_off(db_session, reseller, monkeypatch):
    monkeypatch.setenv("JWT_SECRET", "test-secret")
    old = settings.reseller_user_principal_enabled
    object.__setattr__(settings, "reseller_user_principal_enabled", False)
    _make_reseller_login(db_session, reseller)
    # Flag off → the reseller_user principal isn't resolved, so there is no
    # active principal and login is refused.
    try:
        with pytest.raises(HTTPException) as exc:
            AuthFlow.login(db_session, "abc-admin", "secret", _request(), None)
        assert exc.value.status_code in (401, 403)
    finally:
        object.__setattr__(settings, "reseller_user_principal_enabled", old)


def test_reseller_portal_login_end_to_end(db_session, reseller, flag_on):
    ru = _make_reseller_login(db_session, reseller)
    result = reseller_portal.login(
        db_session, "abc-admin", "secret", _request(), remember=False
    )
    assert result["reseller_id"] == str(reseller.id)
    token = result["session_token"]

    ctx = reseller_portal.get_context(db_session, token)
    assert ctx is not None
    assert ctx["subscriber"] is None  # no backing subscriber
    assert ctx["reseller_user"].id == ru.id
    assert ctx["reseller"].id == reseller.id
    assert ctx["current_user"]["email"] == "owner@abcnetworks.com"
    assert ctx["current_user"]["name"] == "ABC Owner"


def test_primary_totp_lookup_for_reseller_user(db_session, reseller):
    ru = _make_reseller_login(db_session, reseller)
    method = MFAMethod(
        reseller_user_id=ru.id,
        method_type=MFAMethodType.totp,
        is_primary=True,
        enabled=True,
        is_active=True,
    )
    db_session.add(method)
    db_session.commit()
    found = _primary_totp_method(db_session, "reseller_user", str(ru.id))
    assert found is not None
    assert found.id == method.id


def test_subscriber_credentials_still_three_way_valid(db_session, person):
    # The widened 3-way principal constraint must still accept a plain
    # subscriber-only credential (regression).
    cred = UserCredential(
        subscriber_id=person.id,
        provider=AuthProvider.local,
        username="cust-login",
        password_hash=auth_flow_service.hash_password("secret"),
        is_active=True,
    )
    db_session.add(cred)
    db_session.commit()
    assert cred.id is not None
