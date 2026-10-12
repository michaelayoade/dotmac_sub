"""Typed transport and rendered editor checks; no hosted browser or PG claim."""

from collections.abc import Iterator
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

import pytest
from fastapi import FastAPI, HTTPException, Request
from fastapi.routing import APIRoute
from fastapi.testclient import TestClient
from jinja2 import ChoiceLoader, DictLoader, FileSystemLoader
from pydantic import ValidationError
from sqlalchemy.orm import Session

from app.api import support_resolution as api
from app.csrf import CSRF_COOKIE_NAME, CSRF_HEADER_NAME
from app.db import get_db
from app.schemas.support_resolution import ACTION, PERMISSION, ResolutionRequestInput
from app.services import support
from app.services.action_readiness import (
    ActionableBlocker,
    ActionReadiness,
    BlockerEvidence,
    ReadinessState,
)
from app.services.domain_errors import DomainError
from app.services.form_contracts import FORM_CONTRACT_REGISTRY, HIGH_IMPACT_EDITORS
from app.services.permission_admission import PermissionAdmission
from app.services.support_resolution_contracts import (
    SupportResolutionPreview,
    SupportResolutionPreviewQuery,
    SupportResolutionRequestCommand,
    SupportResolutionRequestOutcome,
)
from app.web.admin import support_resolution as web


@dataclass(kw_only=True)
class AdapterRig:
    db: Session
    access: PermissionAdmission
    snapshot: SupportResolutionPreview
    outcome: SupportResolutionRequestOutcome
    commands: list[SupportResolutionRequestCommand] = field(default_factory=list)
    queries: list[SupportResolutionPreviewQuery] = field(default_factory=list)
    refusal: str | None = None


@pytest.fixture
def rig(db_session: Session, monkeypatch: pytest.MonkeyPatch) -> AdapterRig:
    ticket_id, actor_id, request_id = uuid4(), uuid4(), uuid4()
    now = datetime.now(UTC)
    state = AdapterRig(
        db=db_session,
        access=PermissionAdmission(actor_id, "system_user", frozenset({PERMISSION})),
        snapshot=SupportResolutionPreview(
            ticket_id=ticket_id,
            number="TKT-123",
            title="Repair <verified>",
            status="open",
            readiness=ActionReadiness(
                action_key=ACTION,
                subject_type="support_ticket",
                subject_id=str(ticket_id),
                owner="support.ticket_lifecycle",
                state=ReadinessState.ready,
                evaluated_at=now,
            ),
            current_request_id=None,
            requested_at=None,
            deadline=None,
        ),
        outcome=SupportResolutionRequestOutcome(
            request_id=request_id,
            ticket_id=ticket_id,
            requested_at=now,
            deadline=now + timedelta(hours=24),
            status="pending_confirmation",
            replayed=False,
        ),
    )

    def preview(
        db: Session, *, query: SupportResolutionPreviewQuery
    ) -> SupportResolutionPreview:
        state.queries.append(query)
        return state.snapshot

    def command(
        db: Session, *, command: SupportResolutionRequestCommand
    ) -> SupportResolutionRequestOutcome:
        state.commands.append(command)
        if state.refusal:
            raise DomainError(
                code=state.refusal,
                message="Current accepted completion evidence is required",
            )
        return replace(state.outcome, replayed=len(state.commands) > 1)

    def user(request: Request) -> dict[str, str]:
        return {"name": "Staff"}

    def stats(db: Session) -> dict[str, int]:
        return {}

    monkeypatch.setattr(support, "resolution_request_preview", preview)
    monkeypatch.setattr(support, "request_resolution", command)
    monkeypatch.setattr("app.web.admin.get_current_user", user)
    monkeypatch.setattr("app.web.admin.get_sidebar_stats", stats)
    loader = ChoiceLoader(
        [
            DictLoader({"layouts/admin.html": "{% block content %}{% endblock %}"}),
            FileSystemLoader("templates"),
        ]
    )
    monkeypatch.setattr(web.templates.env, "loader", loader)

    def allowed(request: Request, permission: str) -> bool:
        return True

    monkeypatch.setitem(web.templates.env.globals, "can", allowed)
    return state


def client_for(
    rig: AdapterRig, *, browser: bool = False, denied: bool = False
) -> TestClient:
    app = FastAPI()
    router = web.router if browser else api.router
    app.include_router(router, prefix="/admin" if browser else "/api/v1")

    def admission() -> PermissionAdmission:
        if denied:
            raise HTTPException(status_code=403, detail="Forbidden")
        return rig.access

    def database() -> Iterator[Session]:
        yield rig.db

    app.dependency_overrides[get_db] = database
    for route in router.routes:
        assert isinstance(route, APIRoute)
        for dependency in route.dependant.dependencies:
            if dependency.name == "access":
                app.dependency_overrides[dependency.call] = admission
    return TestClient(app)


def body(intent: UUID | None = None) -> dict[str, object]:
    return {
        "intent_id": str(intent or uuid4()),
        "reason": "Verified repair",
        "grace_hours": 24,
        "confirmed": True,
    }


@pytest.mark.parametrize(
    "mode,expected",
    [("missing", 403), ("wrong", 403), ("cookie", 200), ("bearer", 200)],
)
def test_api_real_csrf_dependency_precedes_owner(
    rig: AdapterRig, mode: str, expected: int
) -> None:
    headers = (
        {"authorization": "Bearer opaque"}
        if mode == "bearer"
        else {CSRF_HEADER_NAME: "csrf-test" if mode == "cookie" else "wrong"}
        if mode != "missing"
        else {}
    )
    with client_for(rig) as client:
        client.cookies.set(CSRF_COOKIE_NAME, "csrf-test")
        response = client.post(
            f"/api/v1/support/tickets/{rig.snapshot.ticket_id}/resolution-confirmation",
            json=body(),
            headers=headers,
        )
    assert response.status_code == expected
    assert len(rig.commands) == (1 if expected == 200 else 0)
    if expected == 200:
        assert response.json()["request_id"] == str(rig.outcome.request_id)
        assert "token" not in response.json()


def test_api_preserves_exact_intent_context_and_server_admission(
    rig: AdapterRig,
) -> None:
    intent = uuid4()
    with client_for(rig) as client:
        path = (
            f"/api/v1/support/tickets/{rig.snapshot.ticket_id}/resolution-confirmation"
        )
        first = client.post(
            path, json=body(intent), headers={"authorization": "Bearer opaque"}
        )
        retry = client.post(
            path, json=body(intent), headers={"authorization": "Bearer opaque"}
        )
    assert first.status_code == retry.status_code == 200
    assert retry.json()["replayed"] is True
    assert rig.commands[0] == rig.commands[1]
    command = rig.commands[0]
    assert command.context.command_id == intent
    assert command.context.idempotency_key == str(intent)
    assert command.context.actor == str(rig.access.principal_id)
    assert command.access == rig.access
    assert command.ticket_id == rig.snapshot.ticket_id


@pytest.mark.parametrize(
    "field,value",
    [
        ("access", {}),
        ("actor_id", "11111111-1111-4111-8111-111111111111"),
        ("ticket_id", "22222222-2222-4222-8222-222222222222"),
        ("grace_hours", -1),
        ("grace_hours", 10**20),
        ("reason", " "),
    ],
)
def test_api_invalid_or_injected_input_never_reaches_owner(
    rig: AdapterRig, field: str, value: object
) -> None:
    payload = {**body(), field: value}
    with client_for(rig) as client:
        response = client.post(
            f"/api/v1/support/tickets/{rig.snapshot.ticket_id}/resolution-confirmation",
            json=payload,
            headers={"authorization": "Bearer opaque"},
        )
    assert response.status_code == 422 and not rig.commands


def test_permission_refusal_precedes_preview_and_command(rig: AdapterRig) -> None:
    with client_for(rig, denied=True) as client:
        path = (
            f"/api/v1/support/tickets/{rig.snapshot.ticket_id}/resolution-confirmation"
        )
        assert client.get(path).status_code == 403
        assert (
            client.post(
                path, json=body(), headers={"authorization": "Bearer opaque"}
            ).status_code
            == 403
        )
    assert not rig.queries and not rig.commands


def test_get_is_read_only_and_owner_blocker_disables_editor(rig: AdapterRig) -> None:
    blocker = ActionableBlocker(
        code="operations.field_completion.completion_pending",
        owner="operations.field_completion",
        customer_message="Awaiting review",
        staff_detail="Review the submitted completion evidence",
        evidence=BlockerEvidence("Current submission is pending"),
    )
    rig.snapshot = replace(
        rig.snapshot,
        readiness=replace(
            rig.snapshot.readiness, state=ReadinessState.blocked, blockers=(blocker,)
        ),
    )
    with client_for(rig, browser=True) as client:
        response = client.get(
            f"/admin/support/tickets/{rig.snapshot.ticket_id}/resolution-confirmation"
        )
    assert response.status_code == 200
    assert (
        "Review the submitted completion evidence" in response.text
        and "disabled" in response.text
    )
    assert "TKT-123" in response.text and "Repair &lt;verified&gt;" in response.text
    assert not rig.commands


@pytest.mark.parametrize("csrf", [None, "wrong"])
def test_browser_csrf_refuses_before_owner(rig: AdapterRig, csrf: str | None) -> None:
    data = {
        "intent_id": str(uuid4()),
        "reason": "Verified",
        "grace_hours": "24",
        "confirmed": "yes",
    }
    if csrf is not None:
        data["_csrf_token"] = csrf
    with client_for(rig, browser=True) as client:
        client.cookies.set(CSRF_COOKIE_NAME, "csrf-test")
        response = client.post(
            f"/admin/support/tickets/{rig.snapshot.ticket_id}/resolution-confirmation",
            data=data,
        )
    assert response.status_code == 403 and not rig.commands


@pytest.mark.parametrize(
    "refusal",
    [
        "resolution_basis_changed",
        "resolution_idempotency_conflict",
        "resolution_confirmation_inactive",
    ],
)
def test_browser_owner_refusal_preserves_original_values_and_no_success(
    rig: AdapterRig, refusal: str
) -> None:
    rig.refusal = refusal
    intent = uuid4()
    with client_for(rig, browser=True) as client:
        client.cookies.set(CSRF_COOKIE_NAME, "csrf-test")
        response = client.post(
            f"/admin/support/tickets/{rig.snapshot.ticket_id}/resolution-confirmation",
            data={
                "intent_id": str(intent),
                "reason": "Repair <original>",
                "grace_hours": "36",
                "confirmed": "yes",
                "_csrf_token": "csrf-test",
            },
        )
    assert response.status_code == 409
    assert (
        str(intent) in response.text
        and "Repair &lt;original&gt;" in response.text
        and 'value="36"' in response.text
    )
    assert "Reload current readiness" in response.text and "disabled" in response.text
    assert "Confirmation request recorded" not in response.text
    assert rig.commands[0].context.command_id == intent


def test_browser_validation_and_success_receipt(rig: AdapterRig) -> None:
    intent = uuid4()
    with client_for(rig, browser=True) as client:
        client.cookies.set(CSRF_COOKIE_NAME, "csrf-test")
        path = (
            f"/admin/support/tickets/{rig.snapshot.ticket_id}/resolution-confirmation"
        )
        invalid = client.post(
            path,
            data={
                "intent_id": str(intent),
                "reason": "Original reason",
                "grace_hours": "oops",
                "confirmed": "yes",
                "_csrf_token": "csrf-test",
            },
        )
        assert (
            invalid.status_code == 422
            and str(intent) in invalid.text
            and 'value="oops"' in invalid.text
        )
        assert not rig.commands
        success = client.post(
            path,
            data={
                "intent_id": str(intent),
                "reason": "Original reason",
                "grace_hours": "24",
                "confirmed": "yes",
                "_csrf_token": "csrf-test",
            },
        )
    assert (
        success.status_code == 200 and "Confirmation request recorded" in success.text
    )
    assert "Customer consent has not been recorded" in success.text
    assert 'name="intent_id"' not in success.text


def test_schema_and_form_governance() -> None:
    with pytest.raises(ValidationError):
        ResolutionRequestInput.model_validate({**body(), "confirmed": False})
    assert FORM_CONTRACT_REGISTRY[ACTION].command_owner == "support.ticket_lifecycle"
    assert any(
        entry.contract_key == ACTION and entry.status == "contracted"
        for entry in HIGH_IMPACT_EDITORS
    )


def test_api_owner_refusal_is_an_explicit_conflict(rig: AdapterRig) -> None:
    rig.refusal = "resolution_basis_changed"
    with client_for(rig) as client:
        response = client.post(
            f"/api/v1/support/tickets/{rig.snapshot.ticket_id}/resolution-confirmation",
            json=body(),
            headers={"authorization": "Bearer opaque"},
        )
    assert response.status_code == 409
    assert response.json()["detail"]["code"] == rig.refusal
    assert "request_id" not in response.json()


@pytest.mark.parametrize("intent", ["", " ", "invalid-intent"])
def test_invalid_browser_intent_is_never_replaced_or_executed(
    rig: AdapterRig, intent: str
) -> None:
    with client_for(rig, browser=True) as client:
        client.cookies.set(CSRF_COOKIE_NAME, "csrf-test")
        response = client.post(
            f"/admin/support/tickets/{rig.snapshot.ticket_id}/resolution-confirmation",
            data={
                "intent_id": intent,
                "reason": "Original reason",
                "grace_hours": "24",
                "confirmed": "yes",
                "_csrf_token": "csrf-test",
            },
        )
    assert response.status_code == 422
    assert "disabled" in response.text and "Reload current readiness" in response.text
    if intent.strip():
        assert f'value="{intent}"' in response.text
    assert not rig.commands


def test_thin_adapter_and_ticket_discovery_boundary() -> None:
    import ast
    from pathlib import Path

    for path in (
        "app/api/support_resolution.py",
        "app/web/admin/support_resolution.py",
    ):
        source = Path(path).read_text()
        tree = ast.parse(source)
        assert not any(
            isinstance(node, ast.ImportFrom)
            and (node.module or "").startswith("app.models")
            for node in ast.walk(tree)
        )
        assert "execute_owner_command" not in source
        assert "support.request_resolution(" in source
        assert "support.resolution_request_preview(" in source
        assert not any(
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr in {"commit", "rollback", "begin_nested"}
            for node in ast.walk(tree)
        )
    template = Path("templates/admin/support/tickets/detail.html").read_text()
    assert "can(request, 'support:ticket:update')" in template
    assert "/admin/support/tickets/{{ ticket.id }}/resolution-confirmation" in template
    assert (
        '("app.api.support_resolution", "router", "api", "user")'
        in Path("app/main.py").read_text()
    )
    assert (
        "router.include_router(support_resolution_router)"
        in Path("app/web/admin/__init__.py").read_text()
    )
