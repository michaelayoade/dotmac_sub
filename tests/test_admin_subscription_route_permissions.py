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
from fastapi import FastAPI, Request
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
        "subscription:activate",
        "subscription:suspend",
        "subscription:cancel",
        "support:ticket_service_pause:resume",
        "catalog:read",
        "catalog:write",
    ):
        db_session.add(Permission(key=key, is_active=True))
    db_session.commit()

    def build(scopes: list[str]) -> TestClient:  # noqa: C901
        app = FastAPI()
        app.include_router(admin_catalog.router, prefix="/admin")

        def _db():
            yield db_session

        app.dependency_overrides[get_db] = _db

        def _auth(request: Request) -> dict:
            auth = {
                "principal_type": "system_user",
                "principal_id": str(uuid4()),
                "roles": ["support"],
                "scopes": scopes,
            }
            # The real dependency publishes the principal for in-body checks.
            request.state.auth = auth
            return auth

        app.dependency_overrides[require_user_auth] = _auth
        return TestClient(app, raise_server_exceptions=False)

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
# Mutation routes: subscription:* keys replace the legacy catalog:* guards
# ---------------------------------------------------------------------------

ALL_KEYS = frozenset(
    {
        "subscription:read",
        "subscription:create",
        "subscription:update",
        "subscription:additional_ip:write",
        "subscription:activate",
        "subscription:suspend",
        "subscription:cancel",
        "support:ticket_service_pause:resume",
        "catalog:read",
        "catalog:write",
    }
)

_S = f"/admin/catalog/subscriptions/{SUB_ID}"
_B = "/admin/catalog/subscriptions/bulk"

# (method, path, permissions that each suffice on their own)
MUTATION_ROUTES = [
    (
        "GET",
        "/admin/catalog/subscription-ipv4-addresses?selector=x",
        ("subscription:read",),
    ),
    (
        "GET",
        "/admin/catalog/subscriptions/service-access/available-ipv4",
        ("subscription:update",),
    ),
    ("GET", f"{_S}/access/move", ("subscription:update",)),
    ("POST", f"{_S}/access/move/preview", ("subscription:update",)),
    ("POST", f"{_S}/access/move/confirm", ("subscription:update",)),
    ("POST", f"{_S}/ipv4/replace", ("subscription:update",)),
    ("POST", f"{_S}/ipv4/reconcile", ("subscription:update",)),
    ("POST", f"{_S}/register-radio-mac", ("subscription:update",)),
    ("POST", f"{_S}/force-reauth", ("subscription:update",)),
    ("POST", f"{_S}/send-credentials", ("subscription:update",)),
    (
        "GET",
        "/admin/catalog/subscriptions/ipam/ipv4-available?selector=x",
        ("subscription:read",),
    ),
    ("POST", f"{_S}/lifecycle/schedules/sched-1/cancel", ("subscription:update",)),
    ("POST", f"{_S}/lifecycle/schedules/sched-1/cancel-view", ("subscription:update",)),
    ("POST", f"{_S}/pending-change/req-1/cancel", ("subscription:update",)),
    ("POST", f"{_S}/scheduled-change/req-1/cancel", ("subscription:update",)),
    ("POST", f"{_B}/change-plan", ("subscription:update",)),
    ("GET", "/admin/catalog/bulk-tariff-change", ("subscription:update",)),
    ("POST", "/admin/catalog/bulk-tariff-change/preview", ("subscription:update",)),
    ("POST", "/admin/catalog/bulk-tariff-change/execute", ("subscription:update",)),
    ("POST", f"{_B}/activate", ("subscription:activate",)),
    ("POST", f"{_B}/restore", ("subscription:activate",)),
    ("POST", f"{_S}/resume-vacation-hold", ("subscription:activate",)),
    (
        "POST",
        f"{_S}/ticket-pause/resume",
        ("subscription:activate", "support:ticket_service_pause:resume"),
    ),
    ("POST", f"{_B}/suspend", ("subscription:suspend",)),
    ("POST", f"{_B}/cancel", ("subscription:cancel",)),
    ("POST", f"{_S}/lifecycle/preview", ("subscription:read",)),
    ("POST", f"{_B}/lifecycle/preview", ("subscription:read",)),
    ("GET", f"{_S}/change-plan-quote?target_offer_id=x", ("subscription:read",)),
]


def _route_id(route):
    return f"{route[0]} {route[1].split('?')[0].replace(SUB_ID, '{id}')}"


@pytest.mark.parametrize("route", MUTATION_ROUTES, ids=_route_id)
def test_mutation_route_denied_without_subscription_key(client_for, route):
    method, path, sufficient = route
    # Everything else, including legacy catalog:write/read, is not enough.
    client = client_for(sorted(ALL_KEYS - set(sufficient)))
    response = client.request(method, path, follow_redirects=False)
    assert response.status_code == 403
    assert response.json()["detail"] == "Forbidden"


@pytest.mark.parametrize("route", MUTATION_ROUTES, ids=_route_id)
def test_mutation_route_denied_with_catalog_grants_only(client_for, route):
    method, path, _ = route
    client = client_for(["catalog:read", "catalog:write"])
    response = client.request(method, path, follow_redirects=False)
    assert response.status_code == 403
    assert response.json()["detail"] == "Forbidden"


@pytest.mark.parametrize("route", MUTATION_ROUTES, ids=_route_id)
def test_mutation_route_passes_guard_with_subscription_key(client_for, route):
    method, path, sufficient = route
    for permission in sufficient:
        client = client_for([permission])
        response = client.request(method, path, follow_redirects=False)
        # The handler may reject the stub input (422/4xx/5xx); the guard must not.
        assert response.status_code != 403, (path, permission)
        assert response.status_code != 401, (path, permission)


LIFECYCLE_EXPECTED = {
    "activate": "subscription:activate",
    "restore": "subscription:activate",
    "resume_pause": "subscription:activate",
    "suspend": "subscription:suspend",
    "pause": "subscription:suspend",
    "cancel": "subscription:cancel",
    "disable": "subscription:suspend",
    "renew": "subscription:update",
    "expire": "subscription:cancel",
    "change_plan": "subscription:update",
    "vacation_hold": "subscription:suspend",
    "vacation_resume": "subscription:activate",
}


def _execute(client, kind):
    return client.post(
        f"{_S}/lifecycle/execute",
        data={"kind": kind, "expected_head": "h"},
        headers={"Idempotency-Key": "k"},
        follow_redirects=False,
    )


def test_lifecycle_expectation_covers_every_kind():
    from app.services.subscription_lifecycle import SubscriptionCommandKind

    assert set(LIFECYCLE_EXPECTED) == {kind.value for kind in SubscriptionCommandKind}


@pytest.mark.parametrize(("kind", "required"), sorted(LIFECYCLE_EXPECTED.items()))
def test_lifecycle_execute_enforces_the_requested_action(client_for, kind, required):
    others = sorted((ALL_KEYS - {required}) - {"support:ticket_service_pause:resume"})
    denied = _execute(client_for(others), kind)
    assert denied.status_code == 403
    assert denied.json()["detail"] == "Forbidden"
    allowed = _execute(client_for([required]), kind)
    assert allowed.status_code != 403
    assert allowed.status_code != 401


@pytest.mark.parametrize("kind", ["Cancel", "CANCEL", "obliterate", ""])
def test_lifecycle_execute_rejects_case_variant_or_unknown_kind(client_for, kind):
    response = _execute(client_for(sorted(ALL_KEYS)), kind)
    assert response.status_code == 422


def test_denied_lifecycle_execute_never_reaches_the_service(client_for, monkeypatch):
    calls: list = []
    wf = admin_catalog.web_catalog_subscription_workflows_service
    monkeypatch.setattr(
        wf, "execute_lifecycle_command_response", lambda *a, **k: calls.append(1)
    )
    response = _execute(
        client_for(sorted(ALL_KEYS - {"subscription:cancel"})), "cancel"
    )
    assert response.status_code == 403
    assert calls == []


def test_denied_bulk_cancel_never_reaches_the_service(client_for, monkeypatch):
    calls: list = []
    wf = admin_catalog.web_catalog_subscription_workflows_service
    monkeypatch.setattr(wf, "bulk_cancel_response", lambda *a, **k: calls.append(1))
    client = client_for(["catalog:write", "subscription:update"])
    response = client.post(f"{_B}/cancel", data={"subscription_ids": "x"})
    assert response.status_code == 403
    assert response.json()["detail"] == "Forbidden"
    assert calls == []


CORRECTION_KEYS = (
    "subscription:update",
    "subscription:cancel",
    "subscription:activate",
)


def _correct(client):
    return client.post(
        f"{_S}/correction/execute",
        data={
            "target_subscription_id": SUB_ID,
            "preview_fingerprint": "f",
            "idempotency_key": "k",
            "confirmed": "no",
        },
        follow_redirects=False,
    )


@pytest.mark.parametrize("missing", CORRECTION_KEYS)
def test_correction_execute_needs_all_three_keys(client_for, monkeypatch, missing):
    calls: list = []
    wf = admin_catalog.web_catalog_subscription_workflows_service
    monkeypatch.setattr(
        wf, "execute_subscription_correction_response", lambda *a, **k: calls.append(1)
    )
    held = sorted((set(CORRECTION_KEYS) | {"catalog:write"}) - {missing})
    response = _correct(client_for(held))
    assert response.status_code == 403
    assert response.json()["detail"] == "Forbidden"
    assert calls == []


def test_correction_execute_allowed_with_all_three_keys(client_for):
    response = _correct(client_for(list(CORRECTION_KEYS)))
    assert response.status_code == 303  # unconfirmed -> redirect with error


def test_edit_post_rerender_keeps_update_controls(client_for, monkeypatch):
    captured: dict = {}

    class _Recorder:
        @staticmethod
        def TemplateResponse(_name, context, **_kwargs):  # noqa: N802
            captured.update(context)
            return HTMLResponse("ok")

    monkeypatch.setattr(admin_catalog, "templates", _Recorder)
    wf = admin_catalog.web_catalog_subscription_workflows_service
    monkeypatch.setattr(
        wf,
        "handle_subscription_update_form",
        lambda *a, **k: {
            "form_context": {
                "current_service_password": "pw",
                "subscription": {"id": SUB_ID, "service_password": "pw"},
                "error": "bad",
            }
        },
    )
    response = client_for(["subscription:update"]).post(f"{_S}/edit")
    assert response.status_code == 200
    assert captured["can_manage_subscription_catalog"] is True
    assert captured["can_manage_additional_ip"] is False
    assert captured["current_service_password"] == "pw"


def test_lifecycle_execute_catalog_write_is_not_enough(client_for):
    response = _execute(client_for(["catalog:write", "catalog:read"]), "activate")
    assert response.status_code == 403
    assert response.json()["detail"] == "Forbidden"


def test_lifecycle_execute_without_any_key_is_forbidden_before_body(client_for):
    response = client_for(["subscription:read"]).post(
        f"{_S}/lifecycle/execute", follow_redirects=False
    )
    assert response.status_code == 403
    assert response.json()["detail"] == "Forbidden"


# ---------------------------------------------------------------------------
# Edit page: credentials and action buttons need subscription:update
# ---------------------------------------------------------------------------


@pytest.fixture()
def edit_context(client_for, monkeypatch):
    captured: dict = {}

    class _Recorder:
        @staticmethod
        def TemplateResponse(_name, context, **_kwargs):  # noqa: N802
            captured.update(context)
            return HTMLResponse("ok")

    monkeypatch.setattr(admin_catalog, "templates", _Recorder)
    wf = admin_catalog.web_catalog_subscription_workflows_service
    monkeypatch.setattr(
        wf,
        "subscription_edit_form_context",
        lambda *a, **k: {
            "current_service_password": "s3cret-pw",
            "current_service_login": "user",
            "subscription": {"id": SUB_ID, "service_password": "s3cret-pw"},
        },
    )
    return client_for, captured


def test_edit_page_hides_password_from_additional_ip_writer(edit_context):
    client_for, captured = edit_context
    response = client_for(["subscription:additional_ip:write"]).get(f"{_S}/edit")
    assert response.status_code == 200
    assert captured["can_manage_subscription_catalog"] is False
    assert captured["can_manage_additional_ip"] is True
    assert captured["current_service_password"] == ""
    assert captured["subscription"]["service_password"] == ""


def test_edit_page_shows_password_with_subscription_update(edit_context):
    client_for, captured = edit_context
    response = client_for(["subscription:update"]).get(f"{_S}/edit")
    assert response.status_code == 200
    assert captured["can_manage_subscription_catalog"] is True
    assert captured["current_service_password"] == "s3cret-pw"


def _flag_block(source: str, anchor: str) -> str:
    """Return the template text between the gate opening just before anchor and its endif."""
    start = source.index(anchor)
    open_at = source.rindex("{% if ", 0, start)
    return source[open_at : source.index("{% endif %}", start) + len("{% endif %}")]


@pytest.mark.parametrize(
    ("anchor", "visible_text"),
    [
        ("Current Service Password</label>", "Current Service Password"),
        ("/send-credentials", "Send Credentials"),
        ("/ipv4/replace", "Replace service IPv4 only"),
    ],
)
def test_edit_template_gates_sensitive_controls_on_update(anchor, visible_text):
    source = Path("templates/admin/catalog/subscription_form.html").read_text()
    block = _flag_block(source, anchor)
    assert "can_manage_subscription_catalog" in block.split("%}", 1)[0]
    env = _env()
    context = {"subscription": {"id": "abc"}, "current_service_password": "pw"}
    shown = env.from_string(block).render(
        can_manage_subscription_catalog=True, **context
    )
    hidden = env.from_string(block).render(
        can_manage_subscription_catalog=False, **context
    )
    assert visible_text in shown
    assert visible_text not in hidden


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
