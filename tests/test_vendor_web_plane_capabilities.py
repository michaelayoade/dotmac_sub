"""Capability pins for the /vendor WEB plane.

The API plane (`/api/v1/vendor`) gates every route with
`require_vendor_capability`, and `tests/test_vendor_portal_auth.py` pins it.
The web plane resolves its own context through `_context(auth, db, capability)`
and had no HTTP test at all, so two route-revision handlers silently lost their
`QUOTE_WRITE` gate: a field-role member could author and submit route revisions
through the browser that the API refused. These tests pin the web plane's
capability behaviour directly so the two planes cannot drift apart unnoticed.
"""

from __future__ import annotations

from uuid import uuid4

from fastapi import FastAPI
from fastapi.testclient import TestClient
from starlette.requests import Request
from starlette.responses import HTMLResponse

from app.db import get_db
from app.models.field_vendor import FieldVendor, FieldVendorUser
from app.models.subscriber import UserType
from app.models.system_user import SystemUser
from app.models.vendor_routes import Vendor
from app.web import vendor_portal as vendor_portal_web
from app.web.auth.dependencies import require_web_auth
from app.web.vendor_portal import router


def _member(db_session, role: str) -> SystemUser:
    user = SystemUser(
        first_name="Vendor",
        last_name="Web",
        display_name="Vendor Web",
        email=f"vendor-web-{uuid4().hex[:8]}@example.com",
        user_type=UserType.system_user,
    )
    native = Vendor(name=f"Native Web Co {uuid4().hex[:6]}")
    db_session.add_all([user, native])
    db_session.flush()
    vendor = FieldVendor(
        name="Web Co",
        code=f"VC-{uuid4().hex[:6]}",
        is_active=True,
        crm_vendor_id=str(native.id),
    )
    db_session.add(vendor)
    db_session.flush()
    db_session.add(
        FieldVendorUser(
            vendor_id=vendor.id,
            system_user_id=user.id,
            role=role,
            is_active=True,
        )
    )
    db_session.commit()
    return user


def _client(db_session, user: SystemUser) -> TestClient:
    app = FastAPI()
    app.include_router(router)
    app.dependency_overrides[get_db] = lambda: db_session
    app.dependency_overrides[require_web_auth] = lambda: {
        "principal_id": str(user.id),
        "person_id": str(user.id),
        "subscriber_id": str(user.id),
        "principal_type": "system_user",
        "roles": [],
        "scopes": [],
    }
    return TestClient(app, follow_redirects=False)


def test_field_role_cannot_author_a_route_revision_on_the_web_plane(db_session):
    user = _member(db_session, "field")
    client = _client(db_session, user)

    resp = client.post(
        f"/vendor/projects/{uuid4()}/quotes/{uuid4()}/route-revisions",
        data={"geojson": '{"type": "LineString", "coordinates": [[7.4, 9.0]]}'},
    )

    assert resp.status_code == 403


def test_field_role_cannot_submit_a_route_revision_on_the_web_plane(db_session):
    user = _member(db_session, "field")
    client = _client(db_session, user)

    resp = client.post(f"/vendor/projects/{uuid4()}/route-revisions/{uuid4()}/submit")

    assert resp.status_code == 403


def test_supervisor_clears_the_web_route_revision_capability_gate(db_session):
    """A supervisor holds QUOTE_WRITE, so the capability gate must not refuse.

    The command underneath still rejects the made-up identifiers; the point is
    that the refusal is no longer an authorization one.
    """

    user = _member(db_session, "supervisor")
    client = _client(db_session, user)

    resp = client.post(f"/vendor/projects/{uuid4()}/route-revisions/{uuid4()}/submit")

    assert resp.status_code != 403


def test_vendor_dashboard_uses_independent_project_page_offsets(
    db_session, monkeypatch
):
    user = _member(db_session, "field")
    captured_calls = []

    def fake_list_projects(db, vendor_id, *, available, limit, offset, search):
        captured_calls.append(
            {
                "available": available,
                "limit": limit,
                "offset": offset,
                "search": search,
            }
        )
        return [{"id": uuid4()} for _ in range(limit)]

    def fake_template_response(template_name, context, status_code=200):
        assert template_name == "vendor/dashboard.html"
        assert context["my_projects_page"]["page"] == 2
        assert context["my_projects_page"]["has_next"] is True
        assert context["available_projects_page"]["page"] == 3
        assert context["available_projects_page"]["has_next"] is True
        return HTMLResponse("", status_code=status_code)

    monkeypatch.setattr(
        vendor_portal_web.vendor_portal_operations,
        "list_projects",
        fake_list_projects,
    )
    monkeypatch.setattr(
        vendor_portal_web.templates,
        "TemplateResponse",
        fake_template_response,
    )
    request = Request(
        {
            "type": "http",
            "method": "GET",
            "path": "/vendor",
            "headers": [],
            "query_string": b"my_page=2&available_page=3",
            "server": ("testserver", 80),
            "scheme": "http",
            "client": ("testclient", 50000),
        }
    )

    response = vendor_portal_web.vendor_dashboard(
        request,
        search="alpha",
        my_page=2,
        available_page=3,
        auth={
            "principal_id": str(user.id),
            "person_id": str(user.id),
            "subscriber_id": str(user.id),
            "principal_type": "system_user",
            "roles": [],
            "scopes": [],
        },
        db=db_session,
    )

    assert response.status_code == 200
    assert captured_calls == [
        {"available": False, "limit": 26, "offset": 25, "search": "alpha"},
        {"available": True, "limit": 13, "offset": 24, "search": "alpha"},
    ]
