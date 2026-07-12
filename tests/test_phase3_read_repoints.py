"""Phase 3 PR 8 — read-surface repoints behind the §4.2 per-vertical flags.

Every customer/reseller read surface for projects, quotes and referrals runs
behind ``{vertical}_native_read_enabled`` (``SettingDomain.projects``, default
OFF): OFF keeps serving the CRM mirrors unchanged; ON serves the native
services. Write paths deliberately do NOT flip here — quote-request /
deposit-verify stay on the PR 5 ``quotes_native_write_enabled`` flag and
``POST /me|/portal referrals`` stays a mirror write-through until the §4.3
write flip (PR 14).
"""

from __future__ import annotations

import uuid
from unittest.mock import patch

from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api import me as me_api
from app.db import get_db
from app.models.project import Project
from app.models.project_mirror import ProjectMirror
from app.models.quote_mirror import QuoteMirror
from app.models.subscriber import Reseller, Subscriber
from app.schemas.portal import QuoteRequestCreate, ReferAFriendRequest
from app.services import projects as projects_service
from app.services import referrals as referrals_service
from app.services import reseller_crm_views
from app.services.sales import selfserve as selfserve_service
from app.web.customer import projects as web_projects
from app.web.customer import quotes as web_quotes
from app.web.customer import referrals as web_referrals

# ── fixtures ──────────────────────────────────────────────────────────────────


def _subscriber_principal():
    sid = str(uuid.uuid4())
    return {"principal_type": "subscriber", "subscriber_id": sid}


def _subscriber(db, reseller_id=None) -> Subscriber:
    sub = Subscriber(
        first_name="Cust",
        last_name="Omer",
        email=f"c-{uuid.uuid4().hex[:8]}@example.com",
        reseller_id=reseller_id,
    )
    db.add(sub)
    db.commit()
    db.refresh(sub)
    return sub


def _reseller(db) -> Reseller:
    r = Reseller(name=f"Reseller {uuid.uuid4().hex[:6]}", is_active=True)
    db.add(r)
    db.commit()
    db.refresh(r)
    return r


MIRROR_PROJECTS = {"projects": [], "total": 0, "active": 0, "src": "mirror"}
NATIVE_PROJECTS = {"projects": [], "total": 0, "active": 0, "src": "native"}
MIRROR_QUOTES = {"quotes": [], "total": 0, "open": 0, "src": "mirror"}
NATIVE_QUOTES = {"quotes": [], "total": 0, "open": 0, "src": "native"}
MIRROR_REFERRALS = {"code": "", "referrals": [], "src": "mirror"}
NATIVE_REFERRALS = {"code": "", "referrals": [], "src": "native"}


# ── flag defaults (spec: default OFF until the read flip) ─────────────────────


def test_native_read_flags_default_off(db_session):
    assert projects_service.native_read_enabled(db_session) is False
    assert selfserve_service.native_read_enabled(db_session) is False
    assert referrals_service.native_read_enabled(db_session) is False


def test_read_flags_registered_in_settings_spec():
    from app.models.domain_settings import SettingDomain
    from app.services import settings_spec

    for key in (
        "projects_native_read_enabled",
        "quotes_native_read_enabled",
        "referrals_native_read_enabled",
    ):
        spec = settings_spec.get_spec(SettingDomain.projects, key)
        assert spec is not None, key
        assert spec.default is False


# ── GET /me/projects ──────────────────────────────────────────────────────────


def test_me_projects_flag_off_serves_mirror(monkeypatch):
    principal = _subscriber_principal()
    monkeypatch.setattr(projects_service, "native_read_enabled", lambda db: False)
    monkeypatch.setattr(
        me_api.projects_mirror,
        "read_for_subscriber",
        lambda db, sid: {**MIRROR_PROJECTS, "sid": sid},
    )
    out = me_api.my_projects(db=None, principal=principal)
    assert out["src"] == "mirror"
    assert out["sid"] == principal["subscriber_id"]


def test_me_projects_flag_on_serves_native(monkeypatch):
    principal = _subscriber_principal()
    monkeypatch.setattr(projects_service, "native_read_enabled", lambda db: True)
    monkeypatch.setattr(
        projects_service,
        "portal_read_for_subscriber",
        lambda db, sid: {**NATIVE_PROJECTS, "sid": sid},
    )
    out = me_api.my_projects(db=None, principal=principal)
    assert out["src"] == "native"
    assert out["sid"] == principal["subscriber_id"]


# ── GET /me/quotes ────────────────────────────────────────────────────────────


def test_me_quotes_flag_off_serves_mirror(monkeypatch):
    principal = _subscriber_principal()
    monkeypatch.setattr(selfserve_service, "native_read_enabled", lambda db: False)
    monkeypatch.setattr(
        me_api.quotes_mirror,
        "read_for_subscriber",
        lambda db, sid: {**MIRROR_QUOTES, "sid": sid},
    )
    out = me_api.my_quotes(db=None, principal=principal)
    assert out["src"] == "mirror"
    assert out["sid"] == principal["subscriber_id"]


def test_me_quotes_flag_on_serves_native(monkeypatch):
    principal = _subscriber_principal()
    monkeypatch.setattr(selfserve_service, "native_read_enabled", lambda db: True)
    monkeypatch.setattr(
        selfserve_service.selfserve_quotes,
        "read_for_subscriber",
        lambda db, sid: {**NATIVE_QUOTES, "sid": sid},
    )
    out = me_api.my_quotes(db=None, principal=principal)
    assert out["src"] == "native"
    assert out["sid"] == principal["subscriber_id"]


# ── GET /me/referrals ─────────────────────────────────────────────────────────


def test_me_referrals_flag_off_serves_mirror(monkeypatch):
    principal = _subscriber_principal()
    monkeypatch.setattr(referrals_service, "native_read_enabled", lambda db: False)
    monkeypatch.setattr(
        me_api.referrals_mirror,
        "read_for_subscriber",
        lambda db, sid: {**MIRROR_REFERRALS, "sid": sid},
    )
    out = me_api.my_referrals(db=None, principal=principal)
    assert out["src"] == "mirror"
    assert out["sid"] == principal["subscriber_id"]


def test_me_referrals_flag_on_serves_native(monkeypatch):
    principal = _subscriber_principal()
    monkeypatch.setattr(referrals_service, "native_read_enabled", lambda db: True)
    monkeypatch.setattr(
        referrals_service.referrals,
        "read_for_subscriber",
        lambda db, sid: {**NATIVE_REFERRALS, "sid": sid},
    )
    out = me_api.my_referrals(db=None, principal=principal)
    assert out["src"] == "native"
    assert out["sid"] == principal["subscriber_id"]


# ── write paths do NOT flip with the read flags (PR 5 / PR 14 own them) ───────


def test_me_quote_request_stays_mirror_write_through_with_read_flag_on(monkeypatch):
    principal = _subscriber_principal()
    monkeypatch.setattr(selfserve_service, "native_read_enabled", lambda db: True)
    captured = {}

    def fake_request_quote(db, sid, **kw):
        captured["sid"] = sid
        return {"id": "q1", "status": "draft"}

    monkeypatch.setattr(me_api.quotes_mirror, "request_quote", fake_request_quote)
    out = me_api.my_quote_request(
        payload=QuoteRequestCreate(latitude=9.07, longitude=7.49),
        db=None,
        principal=principal,
    )
    assert out["id"] == "q1"
    assert captured["sid"] == principal["subscriber_id"]


def test_me_refer_a_friend_stays_mirror_write_through_with_read_flag_on(monkeypatch):
    principal = _subscriber_principal()
    monkeypatch.setattr(referrals_service, "native_read_enabled", lambda db: True)
    captured = {}

    def fake_refer(db, sid, **kw):
        captured["sid"] = sid
        return {"id": "r1", "status": "pending", "message": "Referral submitted"}

    def native_refer(db, sid, **kw):  # pragma: no cover - must not run
        raise AssertionError("native refer_a_friend must not run before PR 14")

    monkeypatch.setattr(me_api.referrals_mirror, "refer_a_friend", fake_refer)
    monkeypatch.setattr(referrals_service.referrals, "refer_a_friend", native_refer)
    out = me_api.my_refer_a_friend(
        payload=ReferAFriendRequest(email="friend@example.com"),
        db=None,
        principal=principal,
    )
    assert out["id"] == "r1"
    assert captured["sid"] == principal["subscriber_id"]


def test_web_refer_post_stays_mirror_write_through_with_read_flag_on(
    db_session, monkeypatch
):
    monkeypatch.setattr(referrals_service, "native_read_enabled", lambda db: True)
    app = FastAPI()
    app.include_router(web_referrals.router)
    app.dependency_overrides[get_db] = lambda: db_session
    client = TestClient(app)
    with (
        patch(
            "app.web.customer.referrals.get_current_customer_from_request",
            return_value={"subscriber_id": "s1"},
        ),
        patch(
            "app.web.customer.referrals.referrals_mirror.refer_a_friend",
            return_value={"id": "r2", "status": "pending"},
        ) as refer,
    ):
        r = client.post(
            "/portal/refer-and-earn",
            data={"email": "friend@example.com"},
            follow_redirects=False,
        )
    assert r.status_code == 303
    refer.assert_called_once()


# ── web customer routes (helpers behind the same flags) ───────────────────────


def test_web_tracker_routes_by_flag(monkeypatch):
    monkeypatch.setattr(projects_service, "native_read_enabled", lambda db: False)
    monkeypatch.setattr(
        web_projects.projects_mirror,
        "read_for_subscriber",
        lambda db, sid: MIRROR_PROJECTS,
    )
    assert web_projects._tracker(None, "s1")["src"] == "mirror"

    monkeypatch.setattr(projects_service, "native_read_enabled", lambda db: True)
    monkeypatch.setattr(
        projects_service,
        "portal_read_for_subscriber",
        lambda db, sid: NATIVE_PROJECTS,
    )
    assert web_projects._tracker(None, "s1")["src"] == "native"


def test_web_quotes_routes_by_flag(monkeypatch):
    monkeypatch.setattr(selfserve_service, "native_read_enabled", lambda db: False)
    monkeypatch.setattr(
        web_quotes.quotes_mirror,
        "read_for_subscriber",
        lambda db, sid: MIRROR_QUOTES,
    )
    assert web_quotes._quotes(None, "s1")["src"] == "mirror"

    monkeypatch.setattr(selfserve_service, "native_read_enabled", lambda db: True)
    monkeypatch.setattr(
        selfserve_service.selfserve_quotes,
        "read_for_subscriber",
        lambda db, sid: NATIVE_QUOTES,
    )
    assert web_quotes._quotes(None, "s1")["src"] == "native"


def test_web_referrals_routes_by_flag(monkeypatch):
    monkeypatch.setattr(referrals_service, "native_read_enabled", lambda db: False)
    monkeypatch.setattr(
        web_referrals.referrals_mirror,
        "read_for_subscriber",
        lambda db, sid: MIRROR_REFERRALS,
    )
    assert web_referrals._summary(None, "s1")["src"] == "mirror"

    monkeypatch.setattr(referrals_service, "native_read_enabled", lambda db: True)
    monkeypatch.setattr(
        referrals_service.referrals,
        "read_for_subscriber",
        lambda db, sid: NATIVE_REFERRALS,
    )
    assert web_referrals._summary(None, "s1")["src"] == "native"


# ── reseller aggregation (quotes + projects) ──────────────────────────────────

_FAP_ID = uuid.uuid4()


def _native_quote(db, sub):
    from types import SimpleNamespace

    fap = SimpleNamespace(id=_FAP_ID, name="NAP-041")
    with patch(
        "app.services.sales.selfserve._nearest_fiber_access_point",
        return_value=(fap, 1300.0),
    ):
        return selfserve_service.selfserve_quotes.request_quote(
            db,
            str(sub.id),
            latitude=9.0765,
            longitude=7.3986,
            address="12 Mississippi St, Maitama",
            region="Abuja",
        )


def test_reseller_quotes_flag_off_serves_mirror(db_session, monkeypatch):
    monkeypatch.setattr(selfserve_service, "native_read_enabled", lambda db: False)
    reseller = _reseller(db_session)
    mine = _subscriber(db_session, reseller_id=reseller.id)
    native = _native_quote(db_session, mine)  # must NOT appear while OFF
    db_session.add(
        QuoteMirror(
            crm_quote_id="q-mirror",
            subscriber_id=mine.id,
            status="draft",
            currency="NGN",
            total="75000.00",
            payload={"id": "q-mirror", "status": "draft", "total": "75000.00"},
        )
    )
    db_session.commit()

    out = reseller_crm_views.quotes_for_reseller(db_session, str(reseller.id))
    ids = [q["id"] for q in out["quotes"]]
    assert ids == ["q-mirror"]
    assert str(native.id) not in ids
    assert out["quotes"][0]["account_id"] == str(mine.id)
    assert out["quotes"][0]["account_name"]


def test_reseller_quotes_flag_on_serves_native(db_session, monkeypatch):
    monkeypatch.setattr(selfserve_service, "native_read_enabled", lambda db: True)
    reseller = _reseller(db_session)
    mine = _subscriber(db_session, reseller_id=reseller.id)
    other = _subscriber(db_session)  # not this reseller's
    native = _native_quote(db_session, mine)
    _native_quote(db_session, other)
    db_session.add(
        QuoteMirror(
            crm_quote_id="q-mirror",
            subscriber_id=mine.id,
            status="draft",
            currency="NGN",
        )
    )
    db_session.commit()

    out = reseller_crm_views.quotes_for_reseller(db_session, str(reseller.id))
    ids = [q["id"] for q in out["quotes"]]
    assert ids == [str(native.id)]
    assert out["total"] == 1
    assert out["open"] == 1
    assert out["quotes"][0]["account_id"] == str(mine.id)
    assert out["quotes"][0]["account_name"]


def test_reseller_projects_flag_off_serves_mirror(db_session, monkeypatch):
    monkeypatch.setattr(projects_service, "native_read_enabled", lambda db: False)
    reseller = _reseller(db_session)
    mine = _subscriber(db_session, reseller_id=reseller.id)
    db_session.add(
        ProjectMirror(
            crm_project_id="p-mirror",
            subscriber_id=mine.id,
            name="Install",
            status="active",
            progress_pct=40,
        )
    )
    project = Project(
        name="Native install",
        project_type="fiber_optics_installation",
        status="open",
        subscriber_id=mine.id,
    )
    db_session.add(project)
    db_session.commit()

    out = reseller_crm_views.projects_for_reseller(db_session, str(reseller.id))
    ids = [p["id"] for p in out["projects"]]
    assert ids == ["p-mirror"]
    assert str(project.id) not in ids


def test_reseller_projects_flag_on_serves_native(db_session, monkeypatch):
    monkeypatch.setattr(projects_service, "native_read_enabled", lambda db: True)
    reseller = _reseller(db_session)
    mine = _subscriber(db_session, reseller_id=reseller.id)
    other = _subscriber(db_session)
    project = Project(
        name="Native install",
        project_type="fiber_optics_installation",
        status="open",
        subscriber_id=mine.id,
    )
    foreign = Project(
        name="Other install",
        project_type="fiber_optics_installation",
        status="open",
        subscriber_id=other.id,
    )
    db_session.add_all([project, foreign])
    db_session.add(
        ProjectMirror(
            crm_project_id="p-mirror",
            subscriber_id=mine.id,
            name="Install",
            status="active",
            progress_pct=40,
        )
    )
    db_session.commit()

    out = reseller_crm_views.projects_for_reseller(db_session, str(reseller.id))
    ids = [p["id"] for p in out["projects"]]
    assert ids == [str(project.id)]
    assert out["total"] == 1
    assert out["active"] == 1
    assert out["projects"][0]["account_id"] == str(mine.id)
    assert out["projects"][0]["account_name"]
