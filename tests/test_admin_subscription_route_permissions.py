"""Admin subscription routes are guarded by the subscription:* permissions.

Route -> permission mapping is code; which roles hold the permissions is data
composed in the roles UI. These tests only pin the mapping and the matching
template link visibility.
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

import pytest
from fastapi import FastAPI
from fastapi.responses import HTMLResponse
from fastapi.testclient import TestClient
from jinja2 import Environment, FileSystemLoader

from app.db import get_db
from app.services.auth_dependencies import can, require_user_auth
from app.web.admin import catalog as admin_catalog

SUB_ID = "00000000-0000-0000-0000-000000000001"

# (method, path, permission required)
ROUTES = [
    ("GET", "/admin/catalog/subscriptions", "subscription:read"),
    ("GET", f"/admin/catalog/subscriptions/{SUB_ID}", "subscription:read"),
    ("GET", "/admin/catalog/subscriptions/new", "subscription:create"),
    ("POST", "/admin/catalog/subscriptions", "subscription:create"),
    ("GET", f"/admin/catalog/subscriptions/{SUB_ID}/edit", "subscription:update"),
    ("POST", f"/admin/catalog/subscriptions/{SUB_ID}/edit", "subscription:update"),
]


class _Templates:
    @staticmethod
    def TemplateResponse(*_args, **_kwargs):  # noqa: N802
        return HTMLResponse("ok")


@pytest.fixture()
def client_for(db_session, monkeypatch):
    svc = admin_catalog.web_catalog_subscriptions_service
    wf = admin_catalog.web_catalog_subscription_workflows_service
    monkeypatch.setattr(admin_catalog, "templates", _Templates)
    monkeypatch.setattr(admin_catalog, "_base_context", lambda *a, **k: {})
    monkeypatch.setattr(svc, "subscriptions_list_page_data", lambda *a, **k: {})
    monkeypatch.setattr(svc, "default_subscription_form", lambda *a, **k: {})
    monkeypatch.setattr(svc, "subscription_form_context", lambda *a, **k: {})
    monkeypatch.setattr(wf, "subscription_detail_page_context", lambda *a, **k: {})
    monkeypatch.setattr(wf, "subscription_edit_form_context", lambda *a, **k: {})
    monkeypatch.setattr(
        wf, "handle_subscription_create_form", lambda *a, **k: {"redirect_url": "/x"}
    )
    monkeypatch.setattr(
        wf, "handle_subscription_update_form", lambda *a, **k: {"redirect_url": "/x"}
    )

    from app.models.rbac import Permission

    for key in (
        "subscription:read",
        "subscription:create",
        "subscription:update",
        "subscription:additional_ip:write",
        "catalog:read",
        "catalog:write",
    ):
        db_session.add(Permission(key=key, is_active=True))
    db_session.commit()

    def build(scopes: list[str]) -> TestClient:
        app = FastAPI()
        app.include_router(admin_catalog.router, prefix="/admin")

        def _db():
            yield db_session

        app.dependency_overrides[get_db] = _db
        app.dependency_overrides[require_user_auth] = lambda: {
            "principal_type": "system_user",
            "principal_id": str(uuid4()),
            "roles": ["support"],
            "scopes": scopes,
        }
        return TestClient(app)

    return build


@pytest.mark.parametrize(("method", "path", "permission"), ROUTES)
def test_route_forbidden_without_permission(client_for, method, path, permission):
    # Legacy catalog grants and sibling subscription grants do not suffice.
    others = {"catalog:read", "catalog:write", "subscription:read"} - {permission}
    others |= {"subscription:create", "subscription:update"} - {permission}
    client = client_for(sorted(others))
    response = client.request(method, path, follow_redirects=False)
    assert response.status_code == 403
    assert response.json()["detail"] == "Forbidden"


@pytest.mark.parametrize(("method", "path", "permission"), ROUTES)
def test_route_allowed_with_permission(client_for, method, path, permission):
    client = client_for([permission])
    response = client.request(method, path, follow_redirects=False)
    assert response.status_code in (200, 303)


def test_edit_get_still_open_to_additional_ip_writers(client_for):
    client = client_for(["subscription:additional_ip:write"])
    response = client.get(f"/admin/catalog/subscriptions/{SUB_ID}/edit")
    assert response.status_code == 200


def test_additional_ip_writer_cannot_post_generic_edit(client_for):
    client = client_for(["subscription:additional_ip:write"])
    response = client.post(
        f"/admin/catalog/subscriptions/{SUB_ID}/edit", follow_redirects=False
    )
    assert response.status_code == 403
    assert response.json()["detail"] == "Forbidden"


# ---------------------------------------------------------------------------
# Template link visibility
# ---------------------------------------------------------------------------


def _env() -> Environment:
    env = Environment(loader=FileSystemLoader("templates"), autoescape=True)
    env.globals["can"] = can
    return env


def _request(*keys: str):
    return SimpleNamespace(
        state=SimpleNamespace(auth={"permission_keys": frozenset(keys)})
    )


def _render_snippet(source: str, *keys: str) -> str:
    return _env().from_string(source).render(request=_request(*keys), sid="abc")


def test_ticket_detail_subscription_link_gated_on_read():
    source = Path("templates/admin/support/tickets/detail.html").read_text()
    line = next(
        ln
        for ln in source.splitlines()
        if "/admin/catalog/subscriptions/{{ ticket_linked_pause.subscription_id }}"
        in ln
    )
    snippet = line.replace("ticket_linked_pause.subscription_id", "sid")
    assert "can(request, 'subscription:read')" in snippet
    shown = _render_snippet(snippet, "subscription:read")
    hidden = _render_snippet(snippet, "support:ticket:read")
    assert 'href="/admin/catalog/subscriptions/abc"' in shown
    assert "/admin/catalog/subscriptions/" not in hidden
    assert "abc" in hidden


GATED_TEMPLATES = [
    "templates/admin/support/tickets/detail.html",
    "templates/admin/network/outage_impact.html",
    "templates/admin/network/topology_gaps.html",
    "templates/admin/network/ip-management/index.html",
    "templates/admin/network/ip-management/assignments.html",
    "templates/admin/network/fiber/trace.html",
    "templates/admin/provisioning/detail.html",
    "templates/admin/billing/invoice_detail.html",
    "templates/admin/customers/detail.html",
]


@pytest.mark.parametrize("path", GATED_TEMPLATES)
def test_every_subscription_link_is_behind_a_can_check(path):
    lines = Path(path).read_text().splitlines()
    for number, line in enumerate(lines):
        if 'href="/admin/catalog/subscriptions/' in line and "/new" not in line:
            window = "\n".join(lines[max(0, number - 1) : number + 1])
            assert "can(request, 'subscription:read')" in window, (path, number + 1)
        if 'href="{{ subscription_link }}"' in line:
            window = "\n".join(lines[max(0, number - 1) : number + 1])
            assert "can(request, 'subscription:create')" in window, (path, number + 1)


def test_list_and_detail_action_buttons_gated():
    listing = Path("templates/admin/catalog/subscriptions.html").read_text()
    detail = Path("templates/admin/catalog/subscription_detail.html").read_text()
    assert "{% if can(request, 'subscription:create') %}" in listing
    both = "can(request, 'subscription:update') or can(request, 'subscription:additional_ip:write')"
    assert both in listing
    assert (
        "{% if " + both + " %}"
        '<a href="/admin/catalog/subscriptions/{{ subscription.id }}/edit"' in detail
    )
