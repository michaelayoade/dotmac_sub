"""Tests for admin → reseller impersonation ("view as reseller").

Service-level coverage of the new flow:
- resolving the reseller's login principal,
- minting an impersonation session that ``get_context`` flags correctly,
- the 404s when there is nothing to impersonate, and
- the stop-impersonation redirect + open-redirect guard.
"""

import uuid

import pytest
from fastapi import HTTPException

from app.services import reseller_portal, web_reseller_auth


@pytest.fixture(autouse=True)
def _force_memory_sessions(monkeypatch):
    """Use the in-memory session fallback so we can read sessions back."""
    monkeypatch.setattr("app.services.session_store.get_session_redis", lambda: None)
    reseller_portal._RESELLER_SESSIONS.clear()
    yield
    reseller_portal._RESELLER_SESSIONS.clear()


@pytest.fixture()
def reseller(db_session):
    from app.models.subscriber import Reseller

    r = Reseller(name="Impersonate Co", code=f"IMP{uuid.uuid4().hex[:6]}")
    db_session.add(r)
    db_session.commit()
    db_session.refresh(r)
    return r


def _make_reseller_user(db_session, reseller):
    from app.models.subscriber import ResellerUser

    ru = ResellerUser(
        reseller_id=reseller.id,
        is_active=True,
        email="agent@reseller.test",
        full_name="Reseller Agent",
    )
    db_session.add(ru)
    db_session.commit()
    db_session.refresh(ru)
    return ru


class _FakeRequest:
    def __init__(self, cookies):
        self.cookies = cookies


def test_resolve_principal_returns_active_reseller_user(db_session, reseller):
    ru = _make_reseller_user(db_session, reseller)
    principal = reseller_portal.resolve_impersonation_principal(
        db_session, str(reseller.id)
    )
    assert principal is not None
    assert str(principal.id) == str(ru.id)


def test_resolve_principal_none_when_no_login(db_session, reseller):
    assert (
        reseller_portal.resolve_impersonation_principal(db_session, str(reseller.id))
        is None
    )


def test_create_session_builds_flagged_context(db_session, reseller):
    _make_reseller_user(db_session, reseller)
    return_to = f"/admin/resellers/{reseller.id}"
    token = reseller_portal.create_impersonation_session(
        db_session, reseller_id=str(reseller.id), return_to=return_to
    )

    ctx = reseller_portal.get_context(db_session, token)
    assert ctx is not None
    assert str(ctx["reseller"].id) == str(reseller.id)
    assert ctx["is_impersonation"] is True
    assert ctx["return_to"] == return_to


def test_create_session_404_without_principal(db_session, reseller):
    with pytest.raises(HTTPException) as exc:
        reseller_portal.create_impersonation_session(
            db_session, reseller_id=str(reseller.id), return_to="/admin/resellers"
        )
    assert exc.value.status_code == 404


def test_create_session_404_unknown_reseller(db_session):
    with pytest.raises(HTTPException) as exc:
        reseller_portal.create_impersonation_session(
            db_session, reseller_id=str(uuid.uuid4()), return_to="/admin/resellers"
        )
    assert exc.value.status_code == 404


def test_stop_impersonation_invalidates_and_redirects(monkeypatch, reseller):
    calls = {}
    monkeypatch.setattr(
        web_reseller_auth.reseller_portal,
        "invalidate_session",
        lambda token, db: calls.setdefault("token", token),
    )
    target = f"/admin/resellers/{reseller.id}"
    request = _FakeRequest({reseller_portal.SESSION_COOKIE_NAME: "tok-123"})

    resp = web_reseller_auth.reseller_stop_impersonation(request, target)

    assert calls["token"] == "tok-123"
    assert resp.status_code == 303
    assert resp.headers["location"] == target
    # Cookie cleared via Set-Cookie with an expiry in the past.
    assert reseller_portal.SESSION_COOKIE_NAME in resp.headers.get("set-cookie", "")


def test_stop_impersonation_open_redirect_guard(monkeypatch):
    monkeypatch.setattr(
        web_reseller_auth.reseller_portal,
        "invalidate_session",
        lambda token, db: None,
    )
    request = _FakeRequest({})
    resp = web_reseller_auth.reseller_stop_impersonation(
        request, "https://evil.example/"
    )
    assert resp.headers["location"] == "/admin/resellers"
