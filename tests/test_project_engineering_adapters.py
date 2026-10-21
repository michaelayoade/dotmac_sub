"""Typed fast adapter checks; no PostgreSQL or hosted-browser acceptance claim."""

import re
from collections.abc import Iterator
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime
from decimal import Decimal
from uuid import UUID, uuid4

import pytest
from fastapi import FastAPI, Request
from fastapi.routing import APIRoute
from fastapi.testclient import TestClient
from jinja2 import ChoiceLoader, DictLoader, FileSystemLoader
from pydantic import ValidationError
from sqlalchemy.orm import Session

from app.db import get_db
from app.schemas.infrastructure import InfrastructureReference, InfrastructureType
from app.schemas.project_engineering import (
    EngineeringDraftInput,
    EngineeringFormAction,
    EngineeringFormValues,
    EngineeringIssuanceInput,
    EngineeringPolicyInput,
    browser_input,
)
from app.services import project_engineering_plans as owner
from app.services.permission_admission import PermissionAdmission
from app.services.project_engineering_plan_contracts import (
    APPROVE,
    POLICY,
    READ,
    REVOKE,
    WRITE,
    CurrentEngineeringPlanApproval,
    EngineeringCriterion,
    EngineeringPlanContent,
    EngineeringPlanOutcome,
    EngineeringPolicyView,
    EngineeringReadPurpose,
    EngineeringRevisionView,
    EngineeringTechnology,
    EngineeringWorkspace,
    EngineeringWorkspaceQuery,
    GeoPoint,
    ProjectDispatchPreview,
    ProjectDispatchPreviewQuery,
    PublishApprovalPolicy,
    SurveyedPlanLocation,
)
from app.services.project_engineering_projection import project_page
from app.services.project_work_order_handoff_contracts import PERMISSION
from app.web.admin import project_engineering as web


@dataclass(kw_only=True)
class AdapterRig:
    db: Session
    access: PermissionAdmission
    workspace: EngineeringWorkspace
    preview: ProjectDispatchPreview
    policies: list[PublishApprovalPolicy] = field(default_factory=list)
    reads: list[EngineeringWorkspaceQuery] = field(default_factory=list)
    fail_stale: bool = False


@pytest.fixture
def rig() -> Iterator[AdapterRig]:
    now = datetime.now(UTC)
    project, plan, revision, approval, policy, person = (uuid4() for _ in range(6))
    content = EngineeringPlanContent(
        technology=EngineeringTechnology.fiber,
        infrastructure=InfrastructureReference(
            type=InfrastructureType.cabinet, id=uuid4()
        ),
        location=SurveyedPlanLocation(
            description="Surveyed work site",
            point=GeoPoint(latitude=Decimal("9"), longitude=Decimal("7")),
        ),
        scope_statement="Native infrastructure scope <review>",
        criteria=(
            EngineeringCriterion(
                id=uuid4(),
                order=0,
                statement="Cable secured",
                verification_method="Inspect",
            ),
        ),
    )
    workspace = EngineeringWorkspace(
        project_id=project,
        task_id=None,
        technology=EngineeringTechnology.fiber,
        purpose=EngineeringReadPurpose.policy,
        project_updated_at=now,
        task_updated_at=None,
        infrastructure=content.infrastructure,
        plan_id=plan,
        generation=2,
        policy_generation=1,
        policy=EngineeringPolicyView(
            id=policy,
            revision=1,
            independent_reviewer_required=False,
            reason="Explicit project policy",
            created_at=now,
        ),
        revision=EngineeringRevisionView(
            id=revision,
            revision=1,
            digest="a" * 64,
            author_person_id=person,
            created_at=now,
            content=content,
        ),
        approval_id=approval,
        history=(),
        decisions=(),
        history_offset=0,
        history_has_more=False,
        readiness=owner._readiness("policy", project, None),
        evaluated_at=now,
    )
    current = CurrentEngineeringPlanApproval(
        plan_id=plan,
        project_id=project,
        task_id=None,
        revision_id=revision,
        approval_id=approval,
        policy_revision_id=policy,
        digest="a" * 64,
        content=content,
        author_person_id=person,
        reviewer_person_id=uuid4(),
        generation=2,
    )
    db = Session()
    yield AdapterRig(
        db=db,
        access=PermissionAdmission(
            uuid4(),
            "system_user",
            frozenset({READ, WRITE, APPROVE, REVOKE, POLICY, PERMISSION}),
        ),
        workspace=workspace,
        preview=ProjectDispatchPreview(
            project_id=project,
            task_id=None,
            technology=EngineeringTechnology.fiber,
            project_updated_at=now,
            task_updated_at=None,
            approval=current,
            readiness=owner._readiness("issue", project, None),
            evaluated_at=now,
        ),
    )
    db.close()


@pytest.fixture
def browser(rig: AdapterRig, monkeypatch: pytest.MonkeyPatch) -> Iterator[TestClient]:
    import app.web.admin as admin

    def user(request: Request) -> dict[str, str]:
        return {"name": "Staff"}

    def stats(db: Session) -> dict[str, int]:
        return {}

    def can(request: Request, permission: str) -> bool:
        return permission in rig.access.admitted_permissions

    def read(db: Session, *, query: EngineeringWorkspaceQuery) -> EngineeringWorkspace:
        assert query.access == rig.access
        rig.reads.append(query)
        return replace(
            rig.workspace,
            purpose=query.purpose,
            readiness=owner._readiness(query.purpose.value, query.project_id, None),
        )

    def preview(
        db: Session, *, query: ProjectDispatchPreviewQuery
    ) -> ProjectDispatchPreview:
        assert query.access == rig.access
        return rig.preview

    def publish(
        db: Session, *, command: PublishApprovalPolicy
    ) -> EngineeringPlanOutcome:
        rig.policies.append(command)
        if rig.fail_stale:
            raise owner._error("stale_state")
        return EngineeringPlanOutcome(
            plan_id=None,
            revision_id=None,
            decision_id=None,
            policy_revision_id=uuid4(),
            generation=2,
            action=owner.EngineeringPlanAction.policy_published,
            digest=None,
        )

    def database() -> Session:
        return rig.db

    def admission() -> PermissionAdmission:
        return rig.access

    monkeypatch.setattr(admin, "get_current_user", user)
    monkeypatch.setattr(admin, "get_sidebar_stats", stats)
    monkeypatch.setattr(
        web.templates.env,
        "loader",
        ChoiceLoader(
            [
                DictLoader(
                    {
                        "layouts/admin.html": "<html>{% block content %}{% endblock %}</html>"
                    }
                ),
                FileSystemLoader("templates"),
            ]
        ),
    )
    monkeypatch.setitem(web.templates.env.globals, "can", can)
    monkeypatch.setattr(owner, "read_workspace", read)
    monkeypatch.setattr(owner, "preview_dispatch", preview)
    monkeypatch.setattr(owner, "publish_policy", publish)
    app = FastAPI()
    app.include_router(web.router, prefix="/admin")
    app.dependency_overrides[get_db] = database
    for route in app.routes:
        if isinstance(route, APIRoute):
            for dependency in route.dependant.dependencies:
                if dependency.call is not get_db:
                    app.dependency_overrides[dependency.call] = admission
    with TestClient(app) as client:
        yield client


def url(rig: AdapterRig, action: str) -> str:
    return f"/admin/projects/{rig.workspace.project_id}/engineering/{action}"


def policy_form(browser: TestClient, rig: AdapterRig) -> dict[str, str]:
    response = browser.get(url(rig, "policy"))
    assert response.status_code == 200
    values = dict(re.findall(r'name="([^"]+)"\s+value="([^"]*)"', response.text))
    values.update(
        independent_reviewer_required="false",
        reason="Explicitly allow same Person",
        confirmed="yes",
    )
    return values


def test_policy_requires_explicit_choice(rig: AdapterRig) -> None:
    page = project_page(workspace=replace(rig.workspace, policy=None))
    assert page.form is not None
    assert (
        next(
            field
            for field in page.form.fields
            if field.key == "independent_reviewer_required"
        ).value
        == "select"
    )


def test_policy_submit_uses_server_admission_and_stable_intent(
    browser: TestClient, rig: AdapterRig
) -> None:
    values = policy_form(browser, rig)
    response = browser.post(url(rig, "policy"), data=values, follow_redirects=False)
    assert response.status_code == 303
    assert len(rig.policies) == 1
    command = rig.policies[0]
    assert command.access is rig.access
    assert command.context.command_id == UUID(values["intent_id"])
    assert command.context.idempotency_key == values["intent_id"]
    assert command.independent_reviewer_required is False


def test_csrf_failure_never_invokes_command(
    browser: TestClient, rig: AdapterRig
) -> None:
    values = policy_form(browser, rig)
    values["_csrf_token"] = "wrong"
    assert browser.post(url(rig, "policy"), data=values).status_code == 403
    assert rig.policies == []


def test_stale_form_preserves_original_intent_and_tokens(
    browser: TestClient, rig: AdapterRig
) -> None:
    values = policy_form(browser, rig)
    rig.fail_stale = True
    response = browser.post(url(rig, "policy"), data=values)
    assert response.status_code == 409
    for name in ("intent_id", "expected_project_updated_at", "expected_generation"):
        assert f'name="{name}" value="{values[name]}"' in response.text
    assert "Explicitly allow same Person" in response.text
    assert "Reload current evidence" in response.text
    assert "disabled" in response.text


def test_confirmation_validation_retains_values(
    browser: TestClient, rig: AdapterRig
) -> None:
    values = policy_form(browser, rig)
    values.pop("confirmed")
    response = browser.post(url(rig, "policy"), data=values)
    assert response.status_code == 422
    assert values["intent_id"] in response.text
    assert rig.policies == []


def test_dispatch_only_page_hides_engineering_controls(
    browser: TestClient, rig: AdapterRig
) -> None:
    rig.access = replace(rig.access, admitted_permissions=frozenset({PERMISSION}))
    response = browser.get(url(rig, "issuance"))
    assert response.status_code == 200
    assert "Current approved scope" in response.text
    assert "Native infrastructure scope &lt;review&gt;" in response.text
    assert "Draft revision</a>" not in response.text
    assert rig.reads == []


def test_shared_schema_refuses_injected_admission(rig: AdapterRig) -> None:
    with pytest.raises(ValidationError):
        EngineeringPolicyInput.model_validate(
            {
                "confirmed": True,
                "intent_id": str(uuid4()),
                "reason": "policy",
                "expected_project_updated_at": rig.workspace.project_updated_at,
                "expected_generation": 0,
                "independent_reviewer_required": False,
                "access": {},
            }
        )


def test_draft_criteria_identity_stable_for_exact_retry(rig: AdapterRig) -> None:
    values = EngineeringFormValues(
        intent_id=str(uuid4()),
        reason="Reviewed survey",
        expected_project_updated_at=rig.workspace.project_updated_at.isoformat(),
        expected_generation="2",
        plan_id=str(rig.workspace.plan_id),
        infrastructure_type=rig.workspace.infrastructure.type.value
        if rig.workspace.infrastructure
        else "",
        infrastructure_id=str(rig.workspace.infrastructure.id)
        if rig.workspace.infrastructure
        else "",
        scope_statement="Reviewed work",
        location_kind="surveyed",
        location_description="Physical site",
        latitude="9",
        longitude="7",
        criteria="Cable secured | Inspect",
    )
    first = browser_input(
        action=EngineeringFormAction.draft,
        values=values,
        confirmed=True,
        workspace=rig.workspace,
        preview=None,
    )
    second = browser_input(
        action=EngineeringFormAction.draft,
        values=values,
        confirmed=True,
        workspace=rig.workspace,
        preview=None,
    )
    assert isinstance(first, EngineeringDraftInput) and isinstance(
        second, EngineeringDraftInput
    )
    assert first.content == second.content


def test_issuance_retry_retains_original_evidence_after_revocation(
    rig: AdapterRig,
) -> None:
    page = project_page(preview=rig.preview)
    assert page.form is not None
    raw = {item.key: item.value for item in page.form.hidden_values}
    raw.update({field.key: field.value for field in page.form.fields})
    raw["reason"] = "Reviewed rollout"
    payload = browser_input(
        action=EngineeringFormAction.issue,
        values=EngineeringFormValues.model_validate(raw),
        confirmed=True,
        workspace=None,
        preview=replace(rig.preview, approval=None),
    )
    assert isinstance(payload, EngineeringIssuanceInput)
    assert (
        payload.approval_id == rig.preview.approval.approval_id
        if rig.preview.approval
        else False
    )


def test_issuance_result_links_dispatch_only_with_read_permission(
    browser: TestClient,
    rig: AdapterRig,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.services.project_work_order_handoff_contracts import (
        IssueProjectWorkOrder,
        ProjectWorkOrderIssued,
    )

    def issue(db: Session, *, command: IssueProjectWorkOrder) -> ProjectWorkOrderIssued:
        return ProjectWorkOrderIssued(
            issuance_id=uuid4(),
            work_order_id=uuid4(),
            public_id="WO-12345",
            project_id=command.project_id,
            task_id=command.task_id,
            plan_id=command.plan_id,
            revision_id=command.revision_id,
            approval_id=command.approval_id,
            digest=command.digest,
            command_id=command.context.command_id,
            issued_at=datetime.now(UTC),
        )

    monkeypatch.setattr(web.project_work_order_handoff, "issue", issue)
    rig.access = replace(rig.access, admitted_permissions=frozenset({PERMISSION}))
    page = project_page(preview=rig.preview)
    assert page.form is not None
    raw = {item.key: item.value for item in page.form.hidden_values}
    raw.update({item.key: item.value for item in page.form.fields})
    browser.get(url(rig, "issuance"))
    raw.update(
        _csrf_token=browser.cookies["csrf_token"],
        confirmed="yes",
        reason="Issue exact scope",
    )
    response = browser.post(url(rig, "issuance"), data=raw)
    assert response.status_code == 200
    assert "Issued work order WO-12345" in response.text
    assert "Assignment is a separate action" in response.text
    assert "Open work order for separate dispatch" not in response.text
    rig.access = replace(
        rig.access,
        admitted_permissions=frozenset({PERMISSION, "operations:dispatch:read"}),
    )
    response = browser.post(url(rig, "issuance"), data=raw)
    assert "Open work order for separate dispatch" in response.text


def test_draft_target_preserved_across_retarget_and_exact_retry(
    rig: AdapterRig,
) -> None:
    workspace = replace(
        rig.workspace,
        purpose=EngineeringReadPurpose.draft,
        readiness=owner._readiness("draft", rig.workspace.project_id, None),
    )
    page = project_page(workspace=workspace)
    assert page.form is not None
    values = {item.key: item.value for item in page.form.hidden_values}
    values.update({item.key: item.value for item in page.form.fields})
    values["reason"] = "Survey reviewed"
    raw = EngineeringFormValues.model_validate(values)
    original = browser_input(
        action=EngineeringFormAction.draft,
        values=raw,
        confirmed=True,
        workspace=workspace,
        preview=None,
    )
    changed = replace(
        workspace,
        infrastructure=InfrastructureReference(
            type=InfrastructureType.cabinet, id=uuid4()
        ),
    )
    retried = browser_input(
        action=EngineeringFormAction.draft,
        values=raw,
        confirmed=True,
        workspace=changed,
        preview=None,
    )
    assert isinstance(original, EngineeringDraftInput) and isinstance(
        retried, EngineeringDraftInput
    )
    assert original == retried
    assert original.content.infrastructure == workspace.infrastructure
    assert original.content.infrastructure != changed.infrastructure


def test_new_draft_target_uses_current_native_scope_not_previous_revision(
    rig: AdapterRig,
) -> None:
    current = InfrastructureReference(type=InfrastructureType.cabinet, id=uuid4())
    workspace = replace(
        rig.workspace,
        infrastructure=current,
        purpose=EngineeringReadPurpose.draft,
        readiness=owner._readiness("draft", rig.workspace.project_id, None),
    )
    page = project_page(workspace=workspace)
    assert page.form is not None
    hidden = {item.key: item.value for item in page.form.hidden_values}
    assert hidden["infrastructure_id"] == str(current.id)
