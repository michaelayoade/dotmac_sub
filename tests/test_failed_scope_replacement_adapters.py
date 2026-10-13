"""Fast transport checks; no deployed-schema acceptance claim."""

from dataclasses import replace
from datetime import UTC, datetime
from uuid import uuid4

import pytest
from fastapi import FastAPI
from fastapi.routing import APIRoute
from fastapi.testclient import TestClient
from jinja2 import ChoiceLoader, DictLoader, FileSystemLoader
from pydantic import ValidationError
from sqlalchemy.orm import Session

from app.api import field_completion_review as api
from app.csrf import CSRF_COOKIE_NAME
from app.db import get_db
from app.schemas.field_failed_scope_replacement import (
    FailureInput,
    ReplacementReviewInput,
)
from app.services.action_readiness import ActionReadiness, ReadinessState
from app.services.domain_errors import DomainError
from app.services.field import completion_review as owner
from app.services.field.completion_review_contracts import (
    WRITE,
    FailedScopeReplacementWorkspace,
    IssuedCompletionCriterion,
    ReplacementEnrollmentSnapshot,
    ReplacementWorkspacePurpose,
    UnableToCompleteFailure,
)
from app.services.field_completion_review_projection import replacement_page
from app.services.permission_admission import PermissionAdmission
from app.web.admin import field_completion_review as web


@pytest.fixture
def replacement_state():
    access = PermissionAdmission(uuid4(), "system_user", frozenset({WRITE}))
    criteria = (
        IssuedCompletionCriterion(
            id=uuid4(),
            order=0,
            statement="Restore cable <exact>",
            verification_method="Inspect securing",
        ),
    )
    snapshot = ReplacementEnrollmentSnapshot(
        enrollment_id=uuid4(),
        work_order_public_id="WO-original",
        title="Original failed visit",
        status="canceled",
        criteria_revision_id=uuid4(),
        policy_revision_id=uuid4(),
        assignment_generation=uuid4(),
        criteria=criteria,
        review_generation=2,
        failure_basis=UnableToCompleteFailure(uuid4(), uuid4()),
    )
    candidate = replace(
        snapshot,
        enrollment_id=uuid4(),
        work_order_public_id="WO-revisit",
        title="Cable repair revisit",
        status="scheduled",
        criteria_revision_id=uuid4(),
        criteria=(replace(criteria[0], id=uuid4()),),
        failure_basis=None,
    )
    ready = ActionReadiness(
        action_key="field_completion.review_failed_scope_replacement",
        subject_type="completion_enrollment",
        subject_id="test",
        owner="operations.field_completion",
        state=ReadinessState.ready,
        evaluated_at=datetime.now(UTC),
    )
    return access, FailedScopeReplacementWorkspace(
        source=snapshot,
        expected_generation=0,
        current_decision=None,
        history=(),
        candidates=(candidate,),
        review_readiness=ready,
        revoke_readiness=replace(
            ready, action_key="field_completion.revoke_failed_scope_replacement"
        ),
    )


@pytest.fixture
def replacement_client(replacement_state, monkeypatch):
    access, workspace = replacement_state
    monkeypatch.setattr(
        owner, "failed_scope_replacement_workspace", lambda db, *, query: workspace
    )
    monkeypatch.setattr(
        web.templates.env,
        "loader",
        ChoiceLoader(
            [
                DictLoader({"layouts/admin.html": "{% block content %}{% endblock %}"}),
                FileSystemLoader("templates"),
            ]
        ),
    )
    monkeypatch.setitem(
        web.templates.env.globals,
        "can",
        lambda request, permission: permission == WRITE,
    )
    import app.web.admin as admin

    monkeypatch.setattr(admin, "get_current_user", lambda request: None)
    monkeypatch.setattr(admin, "get_sidebar_stats", lambda db: {})
    app = FastAPI()
    app.include_router(web.router, prefix="/admin")
    app.include_router(api.router, prefix="/api")

    def database():
        with Session() as db:
            yield db

    app.dependency_overrides[get_db] = database
    for router in (web.router, api.router):
        for route in router.routes:
            if isinstance(route, APIRoute):
                for dependency in route.dependant.dependencies:
                    if dependency.name == "access":
                        app.dependency_overrides[dependency.call] = lambda: access
    with TestClient(app) as client:
        yield client


def payload(workspace):
    candidate = workspace.candidates[0]
    page = replacement_page(
        workspace=workspace,
        purpose=ReplacementWorkspacePurpose.review,
        candidate_id=str(candidate.enrollment_id),
    )
    result = dict(page.values.fields)
    result.update(
        _csrf_token="test-token",
        confirmed="yes",
        reason="Reviewed original scope <reason>",
        coverage_0_replacement=str(candidate.criteria[0].id),
    )
    return result


def test_labels_and_explicit_coverage(replacement_client, replacement_state):
    _, workspace = replacement_state
    response = replacement_client.get(
        "/admin/dispatch/work-orders/WO-original/completion-review/replacement",
        params={"candidate_id": str(workspace.candidates[0].enrollment_id)},
    )
    assert response.status_code == 200
    assert "WO-revisit · Cable repair revisit · scheduled" in response.text
    assert "Restore cable &lt;exact&gt;" in response.text
    assert "Select replacement criterion" in response.text
    assert "accepted completion before customer confirmation" in response.text


def test_csrf_runs_before_owner(replacement_client, monkeypatch):
    monkeypatch.setattr(
        owner,
        "review_failed_scope_replacement",
        lambda *a, **kw: pytest.fail("Writer invoked without CSRF"),
    )
    response = replacement_client.post(
        "/admin/dispatch/work-orders/WO-original/completion-review/replacement", data={}
    )
    assert response.status_code == 403


def test_drift_preserves_original_tokens_and_scope(
    replacement_client, replacement_state, monkeypatch
):
    _, workspace = replacement_state
    original = payload(workspace)
    seen = []

    def review(db, *, command):
        seen.append(command)
        raise DomainError(code="completion.stale", message="Assignment changed")

    monkeypatch.setattr(owner, "review_failed_scope_replacement", review)
    replacement_client.cookies.set(CSRF_COOKIE_NAME, "test-token")
    response = replacement_client.post(
        "/admin/dispatch/work-orders/WO-original/completion-review/replacement",
        data=original,
    )
    assert response.status_code == 409
    assert seen[0].expected_work_order_public_id == "WO-original"
    for key in (
        "intent_id",
        "source_assignment_generation",
        "replacement_assignment_generation",
    ):
        assert original[key] in response.text
    assert "Reviewed original scope &lt;reason&gt;" in response.text
    assert "Reload current information" in response.text
    assert "disabled" in response.text


def test_invalid_count_retains_input_without_server_error(
    replacement_client, replacement_state
):
    _, workspace = replacement_state
    original = payload(workspace)
    original["coverage_count"] = "invalid"
    replacement_client.cookies.set(CSRF_COOKIE_NAME, "test-token")
    response = replacement_client.post(
        "/admin/dispatch/work-orders/WO-original/completion-review/replacement",
        data=original,
    )
    assert response.status_code == 422
    assert original["intent_id"] in response.text


def test_failure_shape_and_authority_are_validated():
    with pytest.raises(ValidationError):
        FailureInput(kind="unable_to_complete")
    with pytest.raises(ValidationError):
        ReplacementReviewInput.model_validate({"access": {}})


def test_mutations_require_current_staff_write_and_api_csrf():
    for router in (web.router, api.router):
        for route in router.routes:
            if (
                isinstance(route, APIRoute)
                and "replacement" in route.path
                and "POST" in route.methods
            ):
                assert any(
                    dependency.name == "access"
                    for dependency in route.dependant.dependencies
                )
                if router is api.router:
                    assert any(
                        dependency.call is api.require_browser_csrf_header
                        for dependency in route.dependant.dependencies
                    )


def test_owner_current_authorization_denial_is_mapped(replacement_client, monkeypatch):
    def denied(db, *, query):
        raise DomainError(
            code="completion.denied", message="Current staff permission required"
        )

    monkeypatch.setattr(owner, "failed_scope_replacement_workspace", denied)
    response = replacement_client.get(
        "/admin/dispatch/work-orders/WO-original/completion-review/replacement"
    )
    assert response.status_code == 403
    assert "Current staff permission required" in response.text


def test_review_and_revoke_receipts_are_coverage_only(
    replacement_client, replacement_state, monkeypatch
):
    from app.services.field.completion_review_contracts import (
        FailedScopeReplacementOutcome,
        ReplacementState,
    )

    _, workspace = replacement_state
    receipt = FailedScopeReplacementOutcome(
        source_enrollment_id=workspace.source.enrollment_id,
        replacement_enrollment_id=workspace.candidates[0].enrollment_id,
        decision_id=uuid4(),
        generation=1,
        state=ReplacementState.approved,
    )
    monkeypatch.setattr(
        owner, "review_failed_scope_replacement", lambda db, *, command: receipt
    )
    replacement_client.cookies.set(CSRF_COOKIE_NAME, "test-token")
    response = replacement_client.post(
        "/admin/dispatch/work-orders/WO-original/completion-review/replacement",
        data=payload(workspace),
    )
    assert response.status_code == 200
    assert "Coverage action recorded" in response.text
    assert "This records coverage evidence only" in response.text
    seen = []

    def revoke(db, *, command):
        seen.append(command)
        return replace(receipt, generation=2, state=ReplacementState.revoked)

    monkeypatch.setattr(owner, "revoke_failed_scope_replacement", revoke)
    response = replacement_client.post(
        "/admin/dispatch/work-orders/WO-original/completion-review/replacement/revoke",
        data={
            "_csrf_token": "test-token",
            "confirmed": "yes",
            "intent_id": str(uuid4()),
            "reason": "Coverage withdrawn",
            "source_enrollment_id": str(workspace.source.enrollment_id),
            "expected_generation": "1",
            "decision_id": str(receipt.decision_id),
        },
    )
    assert response.status_code == 200
    assert "Revoked" in response.text
    assert seen[0].decision_id == receipt.decision_id
    assert seen[0].expected_work_order_public_id == "WO-original"


def test_api_cookie_mutation_requires_csrf(
    replacement_client, replacement_state, monkeypatch
):
    from app.csrf import CSRF_HEADER_NAME
    from app.services.field.completion_review_contracts import (
        FailedScopeReplacementOutcome,
        ReplacementState,
    )

    _, workspace = replacement_state
    seen = []

    def revoke(db, *, command):
        seen.append(command)
        return FailedScopeReplacementOutcome(
            source_enrollment_id=command.source_enrollment_id,
            replacement_enrollment_id=workspace.candidates[0].enrollment_id,
            decision_id=command.decision_id,
            generation=2,
            state=ReplacementState.revoked,
        )

    monkeypatch.setattr(owner, "revoke_failed_scope_replacement", revoke)
    body = {
        "confirmed": True,
        "intent_id": str(uuid4()),
        "reason": "Reviewed withdrawal",
        "decision_id": str(uuid4()),
        "expected_generation": 1,
    }
    url = f"/api/completion-review/enrollments/{workspace.source.enrollment_id}/replacement-revocations"
    replacement_client.cookies.set(CSRF_COOKIE_NAME, "test-token")
    assert replacement_client.post(url, json=body).status_code == 403
    assert seen == []
    response = replacement_client.post(
        url, json=body, headers={CSRF_HEADER_NAME: "test-token"}
    )
    assert response.status_code == 200
    assert response.json()["state"] == "revoked"
    assert len(seen) == 1


@pytest.mark.parametrize(
    "failure_stage,code,expected_status",
    [
        ("initial", "completion.scope_mismatch", 409),
        ("reread", "completion.scope_mismatch", 409),
        ("reread", "completion.denied", 403),
    ],
)
def test_unavailable_protected_workspace_keeps_only_original_draft(
    replacement_client,
    replacement_state,
    monkeypatch,
    failure_stage,
    code,
    expected_status,
):
    from app.services.field.completion_review_contracts import (
        FailedScopeReplacementOutcome,
        ReplacementState,
    )

    _, workspace = replacement_state
    calls = []

    def query(db, *, query):
        calls.append(query)
        if failure_stage == "initial" or len(calls) > 1:
            raise DomainError(code=code, message="Current protected scope unavailable")
        return workspace

    monkeypatch.setattr(owner, "failed_scope_replacement_workspace", query)
    writes = []

    def review(db, *, command):
        writes.append(command)
        return FailedScopeReplacementOutcome(
            source_enrollment_id=command.source_enrollment_id,
            replacement_enrollment_id=command.replacement_enrollment_id,
            decision_id=uuid4(),
            generation=1,
            state=ReplacementState.approved,
        )

    monkeypatch.setattr(owner, "review_failed_scope_replacement", review)
    original = payload(workspace)
    replacement_client.cookies.set(CSRF_COOKIE_NAME, "test-token")
    response = replacement_client.post(
        "/admin/dispatch/work-orders/WO-original/completion-review/replacement",
        data=original,
    )
    assert response.status_code == expected_status
    for key in (
        "intent_id",
        "source_assignment_generation",
        "replacement_assignment_generation",
        "source_criteria_revision_id",
        "replacement_criteria_revision_id",
        "coverage_0_replacement",
    ):
        assert original[key] in response.text
    assert "Reviewed original scope &lt;reason&gt;" in response.text
    assert "Reload current information" in response.text
    assert "disabled" in response.text
    assert "Original failed visit" not in response.text
    assert "Cable repair revisit" not in response.text
    assert "Immutable replacement lineage" not in response.text
    assert "Coverage action recorded" not in response.text
    assert len(writes) == (0 if failure_stage == "initial" else 1)


def test_evidence_destination_link_requires_read_permission(
    replacement_client, monkeypatch
):
    url = "/admin/dispatch/work-orders/WO-original/completion-review/replacement"
    response = replacement_client.get(url)
    assert response.status_code == 200
    assert "Back to completion evidence and review" not in response.text
    monkeypatch.setitem(
        web.templates.env.globals, "can", lambda request, permission: True
    )
    response = replacement_client.get(url)
    assert response.status_code == 200
    assert "Back to completion evidence and review" in response.text
