"""Fast project marker/unit behavior; PostgreSQL owns schema and race acceptance."""

from datetime import UTC, datetime
from uuid import uuid4

import pytest
from pydantic import ValidationError
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from app.db import Base
from app.models.project import Project, ProjectInfrastructure, ProjectTask
from app.models.project_field_completion import ProjectFieldCompletionRequirement
from app.schemas.project_field_completion import ApplyAcceptedProjectCompletionRequest
from app.services import projects
from app.services.domain_errors import DomainError
from app.services.project_field_completion_contracts import ProjectFieldScopeQuery
from app.services.project_field_completion_projection import (
    compose_accepted_completion_page,
)


@pytest.fixture
def marked_scope():
    engine = create_engine("sqlite://")
    Base.metadata.create_all(
        engine,
        tables=[
            ProjectInfrastructure.__table__,
            Project.__table__,
            ProjectTask.__table__,
            ProjectFieldCompletionRequirement.__table__,
        ],
    )
    with Session(engine) as db:
        project = Project(id=uuid4(), name="Explicit field project", status="active")
        parent = ProjectTask(
            id=uuid4(), project_id=project.id, title="Parent", status="in_progress"
        )
        child = ProjectTask(
            id=uuid4(),
            project_id=project.id,
            parent_task_id=parent.id,
            title="Child",
            status="in_progress",
        )
        db.add_all([project, parent, child])
        db.flush()
        marker = ProjectFieldCompletionRequirement(
            enrollment_id=uuid4(),
            work_order_id=uuid4(),
            project_id=project.id,
            task_id=child.id,
        )
        db.add(marker)
        db.commit()
        yield db, project, parent, child, marker
    engine.dispose()


@pytest.mark.parametrize("target", ["project", "task", "parent"])
def test_generic_mapped_completion_refuses_every_marked_scope(marked_scope, target):
    db, project, parent, child, _ = marked_scope
    row = {"project": project, "task": child, "parent": parent}[target]
    row.status = "completed" if target == "project" else "done"
    with pytest.raises(DomainError, match="accepted field completion"):
        db.flush()
    db.rollback()


@pytest.mark.parametrize(
    "mutation", ["root_archive", "parent_archive", "child_archive", "reparent"]
)
def test_archival_and_reparent_cannot_escape_requirements(marked_scope, mutation):
    db, project, parent, child, _ = marked_scope
    if mutation == "root_archive":
        project.is_active = False
    elif mutation == "parent_archive":
        parent.is_active = False
    elif mutation == "child_archive":
        child.is_active = False
    else:
        child.parent_task_id = None
    with pytest.raises(
        DomainError, match="cannot be archived|cannot be archived or reparented"
    ):
        db.flush()
    db.rollback()


def test_requirement_token_covers_full_descendant_registry(marked_scope):
    db, project, parent, child, marker = marked_scope
    scope = projects.project_field_completion_scope(
        db, query=ProjectFieldScopeQuery(project_id=project.id, task_id=parent.id)
    )
    assert {r.enrollment_id for r in scope.requirements} == {marker.enrollment_id}
    assert set(scope.task_ids) == {parent.id, child.id}
    assert (
        projects.project_field_completion_scope(
            db, query=ProjectFieldScopeQuery(project_id=project.id, task_id=child.id)
        ).requirements
        == scope.requirements
    )
    assert len(scope.requirement_token) == 64


def test_legacy_unenrolled_mapped_completion_remains_available(marked_scope):
    db, project, _, _, _ = marked_scope
    legacy = Project(id=uuid4(), name="Legacy unenrolled scope", status="active")
    db.add(legacy)
    db.commit()
    legacy.status = "completed"
    db.flush()
    assert legacy.status == "completed"


def test_customer_retarget_invalidates_requirement_scope(marked_scope):
    db, project, _, _, _ = marked_scope
    project.subscriber_id = uuid4()
    db.flush()
    with pytest.raises(DomainError, match="customer"):
        projects.project_field_completion_scope(
            db, query=ProjectFieldScopeQuery(project_id=project.id, task_id=None)
        )


def test_reason_and_preview_tokens_required():
    with pytest.raises(ValidationError):
        ApplyAcceptedProjectCompletionRequest.model_validate(
            dict(
                intent_id=uuid4(),
                command_id=uuid4(),
                correlation_id=uuid4(),
                expected_project_revision=datetime.now(UTC),
                expected_requirement_token="a" * 64,
                reason="   ",
            )
        )


def test_projection_preserves_original_intent_and_tokens_on_stale_error():
    values = {
        "intent_id": str(uuid4()),
        "expected_requirement_token": "b" * 64,
        "reason": "Operator reason",
    }
    page = compose_accepted_completion_page(
        preview=None,
        action_url="/admin/projects/example/accepted-field-completion",
        values=values,
        error="Evidence changed",
    )
    assert dict(page.values) == values
    assert (
        page.form
        and not page.form.allowed
        and page.form.fields[0].value == "Operator reason"
    )
    assert {h.key: h.value for h in page.form.hidden_values} == {
        k: v for k, v in values.items() if k != "reason"
    }


def test_explicit_action_has_registered_owner_boundary():
    from app.services.field.completion_project_scope_contracts import CONCERN
    from app.services.owner_commands import OwnerCommandDefinition, _validate_manifest

    _validate_manifest(
        OwnerCommandDefinition(
            owner="operations.field_completion",
            concern=CONCERN,
            name="ApplyAcceptedProjectCompletion",
        )
    )


def test_adapter_imports_and_template_use_shared_action_contract():
    from pathlib import Path

    from app.api.projects import router as api
    from app.web.admin.projects import router as web

    assert len([r for r in api.routes if "accepted-field-completion" in r.path]) == 4
    assert len([r for r in web.routes if "accepted-field-completion" in r.path]) == 4
    template = Path(
        "templates/admin/projects/accepted_field_completion.html"
    ).read_text()
    assert (
        "action_form(page.form)" in template
        and "action_readiness_panel(page.panel)" in template
    )


def test_historical_inactive_descendant_marker_stays_required(marked_scope):
    from sqlalchemy import text

    db, project, parent, child, marker = marked_scope
    # SQLite fast fixture simulates pre-migration inactive historical task;
    # this is scope traversal evidence, not a SQL guard acceptance claim.
    db.execute(
        text("UPDATE project_tasks SET is_active=false WHERE id=:id"),
        {"id": str(child.id)},
    )
    db.expire_all()
    scope = projects.project_field_completion_scope(
        db, query=ProjectFieldScopeQuery(project_id=project.id, task_id=parent.id)
    )
    assert marker.enrollment_id in {r.enrollment_id for r in scope.requirements}
