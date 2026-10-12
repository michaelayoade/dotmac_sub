"""Fast typed adapter checks; no deployed-schema or hosted-browser claim."""

from collections.abc import Iterator
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from uuid import UUID, uuid4

import pytest
from fastapi import FastAPI, Request
from fastapi.routing import APIRoute
from fastapi.testclient import TestClient
from jinja2 import ChoiceLoader, DictLoader, FileSystemLoader
from pydantic import ValidationError
from sqlalchemy.orm import Session

from app.api import field_completion_review as api
from app.csrf import CSRF_COOKIE_NAME, CSRF_HEADER_NAME
from app.db import get_db
from app.schemas.field_completion_review import (
    CompletionCriteriaInput,
    CompletionCriterionFormValues,
    CompletionDecisionInput,
    CompletionEnrollmentInput,
    CompletionSubmissionInput,
)
from app.services.action_readiness import (
    ActionableBlocker,
    ActionReadiness,
    BlockerEvidence,
    ReadinessState,
)
from app.services.domain_errors import DomainError
from app.services.field import completion_review as owner
from app.services.field.completion_review_contracts import (
    WRITE,
    CompletionEvidenceContent,
    CompletionEvidenceDisplay,
    CompletionEvidenceQuery,
    CompletionPolicyQuery,
    CompletionPolicyScope,
    CompletionPolicyScopeKind,
    CompletionPolicySnapshot,
    CompletionProvenance,
    CompletionReviewHistory,
    CompletionReviewOutcome,
    CompletionSubmissionRecord,
    CompletionWorkspace,
    CompletionWorkspaceQuery,
    CriterionObservation,
    DecideCompletionReview,
    EnrollCompletionReview,
    FieldSubmissionPreview,
    FieldSubmissionPreviewQuery,
    IssuedCompletionCriterion,
    PublishCompletionCriteria,
    PublishCompletionReviewPolicy,
    ReviewState,
    RevokeCompletionReview,
    SubmitCompletionEvidence,
)
from app.services.field_completion_review_projection import (
    CompletionPageAction,
    completion_page,
)
from app.services.object_storage import StreamResult
from app.services.permission_admission import PermissionAdmission
from app.web.admin import field_completion_review as web


def readiness(action: str) -> ActionReadiness:
    return ActionReadiness(
        action_key=f"field_completion.{action}",
        subject_type="completion_enrollment",
        subject_id="test",
        owner="operations.field_completion",
        state=ReadinessState.ready,
        evaluated_at=datetime.now(UTC),
    )


@dataclass(frozen=True, kw_only=True)
class AdapterState:
    access: PermissionAdmission
    workspace: CompletionWorkspace
    preview: FieldSubmissionPreview


@pytest.fixture
def state() -> AdapterState:
    (
        enrollment,
        issuance,
        policy,
        assignment,
        submission,
        criteria_revision,
        completed_event,
    ) = (uuid4() for _ in range(7))
    criterion = IssuedCompletionCriterion(
        id=uuid4(),
        order=0,
        statement="Cable secured <exact>",
        verification_method="Inspect fixture",
    )
    outcome = CompletionReviewOutcome(
        enrollment_id=enrollment,
        policy_revision_id=policy,
        submission_id=submission,
        decision_id=None,
        generation=3,
        state=ReviewState.submitted,
    )
    snapshot = CompletionPolicySnapshot(
        scope=CompletionPolicyScope(
            kind=CompletionPolicyScopeKind.project, project_id=uuid4()
        ),
        revision_id=policy,
        generation=2,
        independent_reviewer_required=False,
        reason="Explicit project choice",
        publish_readiness=readiness("publish_policy"),
    )
    return AdapterState(
        access=PermissionAdmission(uuid4(), "system_user", frozenset({WRITE})),
        workspace=CompletionWorkspace(
            work_order_id="WO-0123",
            project_id=snapshot.scope.project_id,
            task_id=None,
            enrollment_id=enrollment,
            criteria_revision_id=criteria_revision,
            criteria_generation=1,
            completed_event_id=completed_event,
            provenance=CompletionProvenance.engineering_issuance,
            engineering_issuance_id=issuance,
            assignment_generation=assignment,
            current=outcome,
            history=None,
            current_submission=None,
            current_decision=None,
            policy=snapshot,
            criteria=(criterion,),
            decide_readiness=readiness("decide"),
            revoke_readiness=readiness("revoke"),
            enroll_readiness=readiness("enroll"),
            publish_criteria_readiness=readiness("publish_criteria"),
        ),
        preview=FieldSubmissionPreview(
            work_order_id="WO-0123",
            enrollment_id=enrollment,
            criteria_revision_id=criteria_revision,
            criteria_generation=1,
            completed_event_id=completed_event,
            provenance=CompletionProvenance.engineering_issuance,
            engineering_issuance_id=issuance,
            assignment_generation=assignment,
            generation=3,
            policy_revision_id=policy,
            review_state=ReviewState.submitted,
            criteria=(criterion,),
            submission_readiness=readiness("submit_evidence"),
        ),
    )


def test_preview_transport_includes_computed_readiness(
    state: AdapterState, monkeypatch: pytest.MonkeyPatch
) -> None:
    def preview(
        db: Session, *, query: FieldSubmissionPreviewQuery
    ) -> FieldSubmissionPreview:
        assert query.work_order_public_id == "WO-0123"
        assert query.requester_system_user_id == state.access.principal_id
        return state.preview

    monkeypatch.setattr(owner, "field_submission_preview", preview)
    with Session() as db:
        result = api.field_preview(
            "WO-0123", requester=state.access.principal_id, db=db
        )
    assert result.submission_available is True
    assert result.submission_unavailable_reason is None
    assert result.criteria[0].id == state.preview.criteria[0].id


@pytest.mark.parametrize("missing", ["enrollment_id", "assignment_generation"])
def test_preview_returns_owner_blocked_readiness_without_engineering_dead_end(
    state: AdapterState, monkeypatch: pytest.MonkeyPatch, missing: str
) -> None:
    blocked = replace(
        state.preview.submission_readiness,
        state=ReadinessState.blocked,
        blockers=(
            ActionableBlocker(
                code="enrollment_required",
                owner="operations.field_completion",
                customer_message="Ask your supervisor to enroll completion review",
                staff_detail="No enrollment",
                evidence=BlockerEvidence(summary="Not enrolled"),
            ),
        ),
    )
    value = replace(
        state.preview,
        **{
            missing: None,
            "provenance": CompletionProvenance.native_work_order,
            "engineering_issuance_id": None,
            "submission_readiness": blocked,
        },
    )

    def preview(
        db: Session, *, query: FieldSubmissionPreviewQuery
    ) -> FieldSubmissionPreview:
        return value

    monkeypatch.setattr(owner, "field_submission_preview", preview)
    with Session() as db:
        result = api.field_preview(
            "WO-0123", requester=state.access.principal_id, db=db
        )
    assert result.submission_available is False
    assert result.engineering_issuance_id is None
    assert (
        result.submission_unavailable_reason
        == "Ask your supervisor to enroll completion review"
    )


def test_field_submission_keeps_identity_and_public_scope(
    state: AdapterState, monkeypatch: pytest.MonkeyPatch
) -> None:
    intent = uuid4()
    payload = CompletionSubmissionInput(
        confirmed=True,
        intent_id=intent,
        reason="Inspection complete",
        enrollment_id=state.preview.enrollment_id,
        criteria_revision_id=state.preview.criteria_revision_id,
        completed_event_id=state.preview.completed_event_id,
        assignment_generation=state.preview.assignment_generation,
        expected_generation=3,
        policy_revision_id=state.preview.policy_revision_id,
        observations=(
            {
                "criterion_id": state.preview.criteria[0].id,
                "observation": "Measured secure",
                "attachment_ids": [],
            },
        ),
    )
    seen: list[SubmitCompletionEvidence] = []

    def submit(
        db: Session, *, command: SubmitCompletionEvidence
    ) -> CompletionReviewOutcome:
        seen.append(command)
        return state.workspace.current

    monkeypatch.setattr(owner, "submit_evidence", submit)
    with Session() as db:
        api.field_submit("WO-0123", payload, requester=state.access.principal_id, db=db)
        api.field_submit("WO-0123", payload, requester=state.access.principal_id, db=db)
    assert seen[0] == seen[1]
    assert seen[0].expected_work_order_public_id == "WO-0123"
    assert seen[0].context.actor == str(state.access.principal_id)
    assert seen[0].context.command_id == intent


@pytest.mark.parametrize(
    "injected",
    [
        {"access": {}},
        {
            "observations": [
                {
                    "criterion_id": str(uuid4()),
                    "observation": "Done",
                    "verdict": "passed",
                }
            ]
        },
    ],
)
def test_submission_rejects_injected_authority(
    state: AdapterState, injected: dict[str, object]
) -> None:
    data: dict[str, object] = {
        "confirmed": True,
        "intent_id": str(uuid4()),
        "reason": "Observed",
        "enrollment_id": str(state.preview.enrollment_id),
        "criteria_revision_id": str(state.preview.criteria_revision_id),
        "completed_event_id": str(state.preview.completed_event_id),
        "assignment_generation": str(state.preview.assignment_generation),
        "expected_generation": 3,
        "policy_revision_id": str(state.preview.policy_revision_id),
        "observations": [
            {"criterion_id": str(state.preview.criteria[0].id), "observation": "Done"}
        ],
    }
    with pytest.raises(ValidationError):
        CompletionSubmissionInput.model_validate({**data, **injected})


def test_review_has_no_implicit_verdict_and_preserves_stale_input(
    state: AdapterState,
) -> None:
    initial = completion_page(
        action=CompletionPageAction.decide, workspace=state.workspace
    )
    assert initial.values is not None and initial.form is not None
    assert initial.values.results[0].verdict == "select"
    original = initial.values.model_copy(
        update={
            "expected_generation": "1",
            "reason": "Original <reason>",
            "results": (
                CompletionCriterionFormValues(
                    criterion_id=str(state.preview.criteria[0].id),
                    verdict="failed",
                    reason="Original observed failure",
                ),
            ),
        }
    )
    stale = completion_page(
        action=CompletionPageAction.decide,
        workspace=state.workspace,
        submitted=original,
        stale=True,
        error="Generation changed",
    )
    assert stale.values == original
    assert stale.form is not None and not stale.form.allowed
    hidden = {item.key: item.value for item in stale.form.hidden_values}
    assert hidden["intent_id"] == original.intent_id
    assert hidden["expected_generation"] == "1"


def test_decision_transport_is_exact_and_typed(state: AdapterState) -> None:
    payload = CompletionDecisionInput(
        confirmed=True,
        intent_id=uuid4(),
        reason="Reviewed",
        submission_id=state.workspace.current.submission_id,
        expected_generation=3,
        policy_revision_id=state.preview.policy_revision_id,
        results=(
            {
                "criterion_id": state.preview.criteria[0].id,
                "verdict": "passed",
                "reason": "Measured",
            },
        ),
    )
    assert state.workspace.enrollment_id is not None
    command = payload.command(
        access=state.access,
        enrollment_id=state.workspace.enrollment_id,
        work_order_public_id="WO-0123",
    )
    assert command.expected_work_order_public_id == "WO-0123"
    assert command.context.scope == WRITE


@pytest.fixture
def client(
    state: AdapterState, monkeypatch: pytest.MonkeyPatch
) -> Iterator[TestClient]:
    def workspace(
        db: Session, *, query: CompletionWorkspaceQuery
    ) -> CompletionWorkspace:
        assert query.access == state.access
        return state.workspace

    monkeypatch.setattr(owner, "workspace", workspace)
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

    def can(request: Request, permission: str) -> bool:
        return permission == WRITE

    monkeypatch.setitem(web.templates.env.globals, "can", can)
    import app.web.admin as admin

    def current_user(request: Request) -> None:
        return None

    def sidebar(db: Session) -> dict[str, int]:
        return {}

    monkeypatch.setattr(admin, "get_current_user", current_user)
    monkeypatch.setattr(admin, "get_sidebar_stats", sidebar)
    app = FastAPI()
    app.include_router(web.router, prefix="/admin")

    def database() -> Iterator[Session]:
        with Session() as db:
            yield db

    app.dependency_overrides[get_db] = database

    def admission() -> PermissionAdmission:
        return state.access

    for route in web.router.routes:
        assert isinstance(route, APIRoute)
        for dependency in route.dependant.dependencies:
            if dependency.name == "access":
                app.dependency_overrides[dependency.call] = admission
    with TestClient(app) as test_client:
        yield test_client


def test_write_only_review_page_has_exact_criteria_and_separate_execution(
    client: TestClient,
) -> None:
    response = client.get(
        "/admin/dispatch/work-orders/WO-0123/completion-review/review"
    )
    assert response.status_code == 200
    assert "Cable secured &lt;exact&gt;" in response.text
    assert "Select explicit verdict" in response.text
    assert "Field execution completion" in response.text
    assert "Immutable submission and decision history" not in response.text
    assert ">Evidence and history</a>" not in response.text


def test_historical_evidence_uses_its_immutable_criteria_revision(
    client: TestClient,
    state: AdapterState,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    original = replace(
        state.workspace.criteria[0],
        statement="Original retained requirement",
        verification_method="Original verification method",
    )
    historical_revision = uuid4()
    submission = CompletionSubmissionRecord(
        id=uuid4(),
        policy_revision_id=state.workspace.policy.revision_id,
        criteria_revision_id=historical_revision,
        completed_event_id=state.workspace.completed_event_id,
        assignment_generation=state.workspace.assignment_generation,
        actor_system_user_id=state.access.principal_id,
        actor_vendor_user_id=None,
        submitted_at=datetime.now(UTC),
        observations=(
            CriterionObservation(
                criterion_id=original.id, observation="Original retained observation"
            ),
        ),
        criteria=(original,),
    )
    workspace = replace(
        state.workspace,
        history=CompletionReviewHistory(
            enrollment_id=state.workspace.enrollment_id,
            submissions=(submission,),
            decisions=(),
        ),
    )
    monkeypatch.setattr(owner, "workspace", lambda db, *, query: workspace)
    response = client.get("/admin/dispatch/work-orders/WO-0123/completion-review")
    assert response.status_code == 200
    history = response.text.split("Immutable submission and decision history", 1)[1]
    assert "Original retained requirement" in history
    assert "Original verification method" in history
    assert str(historical_revision) in history
    assert "Cable secured" not in history


def test_csrf_refusal_does_not_invoke_writer(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[PublishCompletionReviewPolicy] = []

    def publish(
        db: Session, *, command: PublishCompletionReviewPolicy
    ) -> CompletionReviewOutcome:
        calls.append(command)
        raise AssertionError("CSRF must run before writer")

    monkeypatch.setattr(owner, "publish_policy", publish)
    response = client.post(
        f"/admin/completion-review/projects/{uuid4()}/policy",
        data={"intent_id": str(uuid4())},
    )
    assert response.status_code == 403
    assert not calls


def test_stale_post_keeps_original_intent_and_criterion_inputs(
    client: TestClient, state: AdapterState, monkeypatch: pytest.MonkeyPatch
) -> None:
    intent = uuid4()
    seen: list[DecideCompletionReview] = []

    def decide(
        db: Session, *, command: DecideCompletionReview
    ) -> CompletionReviewOutcome:
        seen.append(command)
        raise DomainError(
            code="operations.field_completion.generation_mismatch",
            message="Reload current evidence",
        )

    monkeypatch.setattr(owner, "decide", decide)
    client.cookies.set(CSRF_COOKIE_NAME, "test-token")
    payload = {
        "_csrf_token": "test-token",
        "confirmed": "yes",
        "intent_id": str(intent),
        "enrollment_id": str(state.workspace.enrollment_id),
        "expected_generation": "1",
        "policy_revision_id": str(state.preview.policy_revision_id),
        "submission_id": str(state.workspace.current.submission_id),
        "reason": "Original <reason>",
        "result_count": "1",
        "result_0_criterion_id": str(state.preview.criteria[0].id),
        "result_0_verdict": "failed",
        "result_0_reason": "Original failure",
    }
    response = client.post(
        "/admin/dispatch/work-orders/WO-0123/completion-review/review", data=payload
    )
    assert response.status_code == 409
    assert str(intent) in response.text
    assert 'name="expected_generation" value="1"' in response.text
    assert (
        "Original &lt;reason&gt;" in response.text
        and "Original failure" in response.text
    )
    assert "Reload current review information" in response.text
    assert "disabled" in response.text
    assert seen[0].expected_work_order_public_id == "WO-0123"
    assert seen[0].context.command_id == intent


@pytest.mark.parametrize("independence", ["true", "false"])
def test_policy_form_records_explicit_choice_without_read_floor(
    client: TestClient,
    state: AdapterState,
    monkeypatch: pytest.MonkeyPatch,
    independence: str,
) -> None:
    assert state.workspace.policy is not None
    policy = state.workspace.policy
    calls: list[PublishCompletionReviewPolicy] = []

    def snapshot(
        db: Session, *, query: CompletionPolicyQuery
    ) -> CompletionPolicySnapshot:
        assert query.access.admitted_permissions == frozenset({WRITE})
        assert query.purpose.value == "publish_policy"
        assert query.scope.kind == CompletionPolicyScopeKind.project
        return policy

    def publish(
        db: Session, *, command: PublishCompletionReviewPolicy
    ) -> CompletionReviewOutcome:
        calls.append(command)
        return state.workspace.current

    monkeypatch.setattr(owner, "policy_snapshot", snapshot)
    monkeypatch.setattr(owner, "publish_policy", publish)
    client.cookies.set(CSRF_COOKIE_NAME, "test-token")
    intent = uuid4()
    response = client.post(
        f"/admin/completion-review/projects/{policy.scope.project_id}/policy",
        data={
            "_csrf_token": "test-token",
            "confirmed": "yes",
            "intent_id": str(intent),
            "expected_generation": "2",
            "independent_reviewer_required": independence,
            "reason": "Explicit project enrollment",
        },
    )
    assert response.status_code == 200
    assert calls[0].independent_reviewer_required is (independence == "true")
    assert calls[0].context.command_id == intent
    assert "Action was recorded" in response.text
    assert "Current review information appears separately" in response.text


def test_retained_evidence_delegates_exact_write_scope_and_private_response(
    state: AdapterState, monkeypatch: pytest.MonkeyPatch
) -> None:
    seen: list[CompletionEvidenceQuery] = []
    attachment, submission, issuance = (uuid4() for _ in range(3))

    def evidence(
        db: Session, *, query: CompletionEvidenceQuery
    ) -> CompletionEvidenceContent:
        seen.append(query)
        return CompletionEvidenceContent(
            evidence=CompletionEvidenceDisplay(
                attachment_id=attachment,
                filename="proof.pdf",
                content_type="application/pdf",
                file_size=5,
                checksum="f" * 64,
            ),
            stream=StreamResult(
                chunks=iter((b"proof",)),
                content_type="application/pdf",
                content_length=5,
            ),
        )

    monkeypatch.setattr(owner, "evidence_content", evidence)
    with Session() as db:
        response = api.review_evidence(
            issuance, submission, attachment, access=state.access, db=db
        )
    assert seen[0].purpose.value == "decide"
    assert (seen[0].enrollment_id, seen[0].submission_id, seen[0].attachment_id) == (
        issuance,
        submission,
        attachment,
    )
    assert response.headers["cache-control"] == "private, no-store"
    assert response.headers["content-length"] == "5"
    assert "proof.pdf" in response.headers["content-disposition"]


@pytest.mark.parametrize(
    "auth_mode,expected",
    [("missing", 403), ("wrong", 403), ("cookie", 200), ("bearer", 200)],
)
def test_real_field_api_csrf_dependency_precedes_owner(
    state: AdapterState, monkeypatch: pytest.MonkeyPatch, auth_mode: str, expected: int
) -> None:
    calls: list[SubmitCompletionEvidence] = []

    def submit(
        db: Session, *, command: SubmitCompletionEvidence
    ) -> CompletionReviewOutcome:
        calls.append(command)
        return state.workspace.current

    def requester() -> UUID:
        return state.access.principal_id

    def database() -> Iterator[Session]:
        with Session() as db:
            yield db

    monkeypatch.setattr(owner, "submit_evidence", submit)
    app = FastAPI()
    app.include_router(api.field_router, prefix="/api/v1")
    app.dependency_overrides[api.field_requester] = requester
    app.dependency_overrides[get_db] = database
    headers = (
        {"authorization": "Bearer test-opaque"}
        if auth_mode == "bearer"
        else {CSRF_HEADER_NAME: "test-token" if auth_mode == "cookie" else "wrong"}
        if auth_mode != "missing"
        else {}
    )
    intent = uuid4()
    with TestClient(app) as transport:
        transport.cookies.set(CSRF_COOKIE_NAME, "test-token")
        response = transport.post(
            "/api/v1/field/jobs/WO-0123/completion-review/submissions",
            headers=headers,
            json={
                "confirmed": True,
                "intent_id": str(intent),
                "reason": "Observed secure",
                "enrollment_id": str(state.preview.enrollment_id),
                "criteria_revision_id": str(state.preview.criteria_revision_id),
                "completed_event_id": str(state.preview.completed_event_id),
                "assignment_generation": str(state.preview.assignment_generation),
                "expected_generation": 3,
                "policy_revision_id": str(state.preview.policy_revision_id),
                "observations": [
                    {
                        "criterion_id": str(state.preview.criteria[0].id),
                        "observation": "Measured secure",
                        "attachment_ids": [],
                    }
                ],
            },
        )
    assert response.status_code == expected
    assert len(calls) == (1 if expected == 200 else 0)
    if calls:
        assert calls[0].context.command_id == intent
        assert calls[0].expected_work_order_public_id == "WO-0123"


@pytest.mark.parametrize(
    "action", ["policy", "decision", "revoke", "work_policy", "enroll", "criteria"]
)
@pytest.mark.parametrize(
    "auth_mode,expected",
    [("missing", 403), ("wrong", 403), ("cookie", 200), ("bearer", 200)],
)
def test_real_staff_api_csrf_dependency_precedes_owner(
    state: AdapterState,
    monkeypatch: pytest.MonkeyPatch,
    action: str,
    auth_mode: str,
    expected: int,
) -> None:
    calls: list[
        PublishCompletionReviewPolicy
        | DecideCompletionReview
        | RevokeCompletionReview
        | EnrollCompletionReview
        | PublishCompletionCriteria
    ] = []

    def publish(
        db: Session, *, command: PublishCompletionReviewPolicy
    ) -> CompletionReviewOutcome:
        calls.append(command)
        return state.workspace.current

    def decide(
        db: Session, *, command: DecideCompletionReview
    ) -> CompletionReviewOutcome:
        calls.append(command)
        return state.workspace.current

    def revoke(
        db: Session, *, command: RevokeCompletionReview
    ) -> CompletionReviewOutcome:
        calls.append(command)
        return state.workspace.current

    def enroll(
        db: Session, *, command: EnrollCompletionReview
    ) -> CompletionReviewOutcome:
        calls.append(command)
        return state.workspace.current

    def publish_criteria(
        db: Session, *, command: PublishCompletionCriteria
    ) -> CompletionReviewOutcome:
        calls.append(command)
        return state.workspace.current

    def admission() -> PermissionAdmission:
        return state.access

    def database() -> Iterator[Session]:
        with Session() as db:
            yield db

    monkeypatch.setattr(owner, "publish_policy", publish)
    monkeypatch.setattr(owner, "decide", decide)
    monkeypatch.setattr(owner, "revoke", revoke)
    monkeypatch.setattr(owner, "enroll", enroll)
    monkeypatch.setattr(owner, "publish_criteria", publish_criteria)
    app = FastAPI()
    app.include_router(api.router, prefix="/api/v1")
    app.dependency_overrides[get_db] = database
    for route in api.router.routes:
        assert isinstance(route, APIRoute)
        for dependency in route.dependant.dependencies:
            if dependency.name == "access":
                app.dependency_overrides[dependency.call] = admission
    headers = (
        {"authorization": "Bearer test-opaque"}
        if auth_mode == "bearer"
        else {CSRF_HEADER_NAME: "test-token" if auth_mode == "cookie" else "wrong"}
        if auth_mode != "missing"
        else {}
    )
    intent = uuid4()
    body: dict[str, object] = {
        "confirmed": True,
        "intent_id": str(intent),
        "reason": "Explicit staff review",
        "expected_generation": 3,
    }
    if action in {"policy", "work_policy"}:
        body["independent_reviewer_required"] = False
        path = (
            f"/projects/{uuid4()}/policy"
            if action == "policy"
            else "/work-orders/WO-0123/policy"
        )
    elif action == "decision":
        body.update(
            {
                "submission_id": str(state.workspace.current.submission_id),
                "policy_revision_id": str(state.preview.policy_revision_id),
                "results": [
                    {
                        "criterion_id": str(state.preview.criteria[0].id),
                        "verdict": "passed",
                        "reason": "Measured",
                    }
                ],
            }
        )
        path = f"/enrollments/{state.workspace.enrollment_id}/decisions"
    elif action == "enroll":
        body.pop("expected_generation")
        body.update(
            {
                "provenance": "native_work_order",
                "policy_scope": {
                    "kind": "work_order",
                    "work_order_public_id": "WO-0123",
                },
                "criteria": [
                    {
                        "id": str(uuid4()),
                        "order": 0,
                        "statement": "Operator requirement",
                        "verification_method": "Operator verification",
                    }
                ],
            }
        )
        path = "/work-orders/WO-0123/enrollment"
    elif action == "criteria":
        body["criteria"] = [
            {
                "id": str(uuid4()),
                "order": 0,
                "statement": "Revised requirement",
                "verification_method": "Revised verification",
            }
        ]
        path = f"/enrollments/{state.workspace.enrollment_id}/criteria"
    else:
        body["decision_id"] = str(uuid4())
        path = f"/enrollments/{state.workspace.enrollment_id}/revocations"
    with TestClient(app) as transport:
        transport.cookies.set(CSRF_COOKIE_NAME, "test-token")
        response = transport.post(
            "/api/v1/completion-review" + path, json=body, headers=headers
        )
    assert response.status_code == expected
    assert len(calls) == (1 if expected == 200 else 0)
    if calls:
        assert calls[0].context.command_id == intent


def test_withdrawal_labels_recorded_decision_without_claiming_fresh_acceptance(
    client: TestClient,
) -> None:
    response = client.get(
        "/admin/dispatch/work-orders/WO-0123/completion-review/revoke"
    )
    assert response.status_code == 200
    assert "Recorded supervisor decision" in response.text
    assert "does not establish current engineering acceptance" in response.text
    assert "Current supervisor review</h2>" not in response.text


@pytest.fixture
def native_workspace(state: AdapterState) -> CompletionWorkspace:
    assert state.workspace.policy is not None
    policy = replace(
        state.workspace.policy,
        scope=CompletionPolicyScope(
            kind=CompletionPolicyScopeKind.work_order, work_order_public_id="WO-0123"
        ),
    )
    return replace(
        state.workspace,
        project_id=None,
        engineering_issuance_id=None,
        provenance=CompletionProvenance.native_work_order,
        policy=policy,
    )


def test_native_policy_enrollment_and_versioned_criteria_are_typed_and_have_no_fake_issuance(
    state: AdapterState,
    native_workspace: CompletionWorkspace,
) -> None:
    assert native_workspace.policy is not None
    enrollment_page = completion_page(
        action=CompletionPageAction.enroll,
        workspace=replace(
            native_workspace,
            enrollment_id=None,
            criteria_revision_id=None,
            criteria_generation=0,
            criteria=(),
        ),
        criterion_rows=2,
    )
    assert enrollment_page.form is not None and enrollment_page.form.allowed
    assert enrollment_page.values is not None
    assert len(enrollment_page.values.criteria) == 2
    assert all(
        not item.statement and not item.verification_method
        for item in enrollment_page.values.criteria
    )
    intent = uuid4()
    criterion = native_workspace.criteria[0]
    command = CompletionEnrollmentInput(
        confirmed=True,
        intent_id=intent,
        reason="Explicit native review",
        provenance=CompletionProvenance.native_work_order,
        policy_scope=native_workspace.policy.scope,
        criteria=(
            {
                "id": criterion.id,
                "order": 0,
                "statement": criterion.statement,
                "verification_method": criterion.verification_method,
            },
        ),
    ).command(access=state.access, work_order_public_id="WO-0123")
    assert command.context.command_id == intent
    assert command.engineering_issuance_id is None
    assert command.policy_scope.work_order_public_id == "WO-0123"
    assert command.criteria == (criterion,)
    criteria_page = completion_page(
        action=CompletionPageAction.publish_criteria, workspace=native_workspace
    )
    assert criteria_page.values is not None
    assert criteria_page.values.expected_generation == str(
        native_workspace.criteria_generation
    )
    revised = CompletionCriteriaInput(
        confirmed=True,
        intent_id=uuid4(),
        reason="Operator revision",
        expected_generation=native_workspace.criteria_generation,
        criteria=(
            {
                "id": criterion.id,
                "order": 0,
                "statement": "New requirement",
                "verification_method": "New method",
            },
        ),
    ).command(
        access=state.access,
        enrollment_id=native_workspace.enrollment_id,
        work_order_public_id="WO-0123",
    )
    assert revised.expected_work_order_public_id == "WO-0123"
    assert revised.criteria[0].statement == "New requirement"


def test_native_enrollment_page_exposes_policy_and_authored_rows_without_read_floor(
    client: TestClient,
    native_workspace: CompletionWorkspace,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def workspace(
        db: Session, *, query: CompletionWorkspaceQuery
    ) -> CompletionWorkspace:
        assert query.purpose.value == "enroll"
        assert query.access.admitted_permissions == frozenset({WRITE})
        return replace(native_workspace, enrollment_id=None, criteria=())

    monkeypatch.setattr(owner, "workspace", workspace)
    response = client.get(
        "/admin/dispatch/work-orders/WO-0123/completion-review/enroll?criterion_rows=2"
    )
    assert response.status_code == 200
    assert 'name="criterion_0_statement"' in response.text
    assert 'name="criterion_1_verification_method"' in response.text
    assert 'name="provenance" value="native_work_order"' in response.text
    assert "/completion-review/policy" in response.text
    assert "no immutable project engineering issuance" not in response.text


@pytest.mark.parametrize("action", ["enroll", "criteria", "policy"])
def test_native_web_csrf_refusal_precedes_every_writer(
    client: TestClient, monkeypatch: pytest.MonkeyPatch, action: str
) -> None:
    def forbidden(
        db: Session,
        *,
        command: EnrollCompletionReview
        | PublishCompletionCriteria
        | PublishCompletionReviewPolicy,
    ) -> CompletionReviewOutcome:
        raise AssertionError("CSRF must precede every native writer")

    monkeypatch.setattr(owner, "enroll", forbidden)
    monkeypatch.setattr(owner, "publish_criteria", forbidden)
    monkeypatch.setattr(owner, "publish_policy", forbidden)
    response = client.post(
        f"/admin/dispatch/work-orders/WO-0123/completion-review/{action}",
        data={"intent_id": str(uuid4())},
    )
    assert response.status_code == 403


def test_native_criteria_stale_post_preserves_authored_values_and_original_generation(
    client: TestClient,
    state: AdapterState,
    native_workspace: CompletionWorkspace,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seen: list[PublishCompletionCriteria] = []

    def workspace(
        db: Session, *, query: CompletionWorkspaceQuery
    ) -> CompletionWorkspace:
        return native_workspace

    def publish(
        db: Session, *, command: PublishCompletionCriteria
    ) -> CompletionReviewOutcome:
        seen.append(command)
        raise DomainError(
            code="operations.field_completion.generation_mismatch",
            message="Criteria changed; reload",
        )

    monkeypatch.setattr(owner, "workspace", workspace)
    monkeypatch.setattr(owner, "publish_criteria", publish)
    client.cookies.set(CSRF_COOKIE_NAME, "token")
    intent = uuid4()
    criterion = uuid4()
    response = client.post(
        "/admin/dispatch/work-orders/WO-0123/completion-review/criteria",
        data={
            "_csrf_token": "token",
            "confirmed": "yes",
            "intent_id": str(intent),
            "enrollment_id": str(native_workspace.enrollment_id),
            "expected_generation": "0",
            "reason": "Original <criterion revision>",
            "criterion_count": "1",
            "criterion_0_id": str(criterion),
            "criterion_0_statement": "Original <requirement>",
            "criterion_0_verification_method": "Original verification",
        },
    )
    assert response.status_code == 409
    assert seen[0].expected_generation == 0
    assert seen[0].expected_work_order_public_id == "WO-0123"
    assert seen[0].context.command_id == intent
    assert "Original &lt;requirement&gt;" in response.text
    assert 'name="expected_generation" value="0"' in response.text
    assert "Reload current review information" in response.text


@pytest.mark.parametrize("action", ["enroll", "policy"])
def test_native_web_writes_pass_exact_scope_and_original_intent(
    client: TestClient,
    state: AdapterState,
    native_workspace: CompletionWorkspace,
    monkeypatch: pytest.MonkeyPatch,
    action: str,
) -> None:
    seen: list[EnrollCompletionReview | PublishCompletionReviewPolicy] = []

    def workspace(
        db: Session, *, query: CompletionWorkspaceQuery
    ) -> CompletionWorkspace:
        return native_workspace

    def policy(
        db: Session, *, query: CompletionPolicyQuery
    ) -> CompletionPolicySnapshot:
        assert query.scope.work_order_public_id == "WO-0123"
        assert native_workspace.policy is not None
        return native_workspace.policy

    def write(
        db: Session, *, command: EnrollCompletionReview | PublishCompletionReviewPolicy
    ) -> CompletionReviewOutcome:
        seen.append(command)
        assert native_workspace.current is not None
        return native_workspace.current

    monkeypatch.setattr(owner, "workspace", workspace)
    monkeypatch.setattr(owner, "policy_snapshot", policy)
    monkeypatch.setattr(owner, "enroll", write)
    monkeypatch.setattr(owner, "publish_policy", write)
    client.cookies.set(CSRF_COOKIE_NAME, "token")
    intent = uuid4()
    criterion = native_workspace.criteria[0]
    response = client.post(
        f"/admin/dispatch/work-orders/WO-0123/completion-review/{action}",
        data={
            "_csrf_token": "token",
            "confirmed": "yes",
            "intent_id": str(intent),
            "reason": "Explicit native choice",
            "provenance": "native_work_order",
            "policy_scope_kind": "work_order",
            "policy_work_order_public_id": "WO-0123",
            "expected_generation": "2",
            "independent_reviewer_required": "false",
            "criterion_count": "1",
            "criterion_0_id": str(criterion.id),
            "criterion_0_statement": criterion.statement,
            "criterion_0_verification_method": criterion.verification_method,
        },
    )
    assert response.status_code == 200
    command = seen[0]
    assert command.context.command_id == intent
    assert command.access == state.access
    if isinstance(command, EnrollCompletionReview):
        assert command.policy_scope.work_order_public_id == "WO-0123"
        assert command.engineering_issuance_id is None
        assert command.criteria == native_workspace.criteria
    else:
        assert command.scope.work_order_public_id == "WO-0123"
        assert command.expected_generation == 2
        assert command.independent_reviewer_required is False
