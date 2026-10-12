"""Protected owner read behavior, non-authoritative SQLite fast lane."""

from collections.abc import Iterator
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime
from uuid import UUID, uuid4

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from app.db import Base
from app.models.project_engineering_plan import (
    EngineeringPlanCriterion,
    EngineeringPlanDecision,
    EngineeringPlanRevision,
    EngineeringPolicyHead,
    EngineeringPolicyRevision,
    ProjectEngineeringPlan,
)
from app.schemas.infrastructure import InfrastructureReference, InfrastructureType
from app.services import project_engineering_plans as owner
from app.services.permission_admission import PermissionAdmission
from app.services.project_engineering_plan_contracts import (
    APPROVE,
    READ,
    REVOKE,
    WRITE,
    CompletionEngineeringPlanQuery,
    EngineeringPlanContent,
    EngineeringReadPurpose,
    EngineeringTechnology,
    EngineeringWorkspaceQuery,
    ProjectDispatchPreviewQuery,
)
from app.services.projects import (
    ProjectEngineeringOrigin,
    ProjectEngineeringRevocationScope,
)
from app.services.staff_permission_admission import AuthorizedStaffPerson


@dataclass(kw_only=True)
class ReadRig:
    db: Session
    origin: ProjectEngineeringOrigin
    person: UUID
    permissions: list[str] = field(default_factory=list)
    archived: bool = False
    expire_after_wait: bool = False

    def access(self, permission: str) -> PermissionAdmission:
        return PermissionAdmission(uuid4(), "system_user", frozenset({permission}))


@pytest.fixture
def rig(monkeypatch: pytest.MonkeyPatch) -> Iterator[ReadRig]:
    engine = create_engine("sqlite://")
    Base.metadata.create_all(
        engine,
        tables=[
            model.__table__
            for model in (
                ProjectEngineeringPlan,
                EngineeringPlanRevision,
                EngineeringPlanCriterion,
                EngineeringPlanDecision,
                EngineeringPolicyHead,
                EngineeringPolicyRevision,
            )
        ],
    )
    now = datetime.now(UTC)
    state = ReadRig(
        db=Session(engine),
        person=uuid4(),
        origin=ProjectEngineeringOrigin(
            project_id=uuid4(),
            task_id=uuid4(),
            project_updated_at=now,
            task_updated_at=now,
            project_template_id=None,
            applied_template_revision=None,
            task_template_id=None,
            task_template_revision=None,
            infrastructure=InfrastructureReference(
                type=InfrastructureType.cabinet, id=uuid4()
            ),
            issuance_eligible=True,
        ),
    )

    def authorize(
        db: Session, *, admission: PermissionAdmission, permission: str, write: bool
    ) -> AuthorizedStaffPerson:
        state.permissions.append(permission)
        if permission not in admission.admitted_permissions or (
            state.expire_after_wait and len(state.permissions) > 1
        ):
            raise owner._error("denied")
        return AuthorizedStaffPerson(admission.principal_id, state.person)

    def historical(
        db: Session, *, project_id: UUID, task_id: UUID | None
    ) -> ProjectEngineeringRevocationScope:
        assert project_id == state.origin.project_id and task_id == state.origin.task_id
        return ProjectEngineeringRevocationScope(
            project_id=project_id,
            task_id=task_id,
            project_updated_at=now,
            task_updated_at=now,
        )

    def origin(
        db: Session, *, project_id: UUID, task_id: UUID | None, lock: bool
    ) -> ProjectEngineeringOrigin:
        if state.archived:
            raise owner._error("stale_state")
        return state.origin

    monkeypatch.setattr(owner, "revalidate_staff_admission", authorize)
    monkeypatch.setattr(owner, "engineering_plan_revocation_scope", historical)
    monkeypatch.setattr(owner, "engineering_plan_origin", origin)
    yield state
    state.db.close()
    engine.dispose()


@pytest.mark.parametrize(
    ("purpose", "permission"),
    [
        (EngineeringReadPurpose.overview, READ),
        (EngineeringReadPurpose.draft, WRITE),
        (EngineeringReadPurpose.review, APPROVE),
        (EngineeringReadPurpose.revoke, REVOKE),
    ],
)
def test_action_read_uses_its_exact_capability(
    rig: ReadRig, purpose: EngineeringReadPurpose, permission: str
) -> None:
    snapshot = owner.read_workspace(
        rig.db,
        query=EngineeringWorkspaceQuery(
            access=rig.access(permission),
            project_id=rig.origin.project_id,
            task_id=rig.origin.task_id,
            technology=EngineeringTechnology.fiber,
            purpose=purpose,
        ),
    )
    assert rig.permissions == [permission, permission]
    assert snapshot.history == () and snapshot.decisions == ()
    assert snapshot.policy is None
    if purpose in {EngineeringReadPurpose.review, EngineeringReadPurpose.revoke}:
        assert not snapshot.readiness.is_ready


def test_archived_revocation_view_does_not_require_active_origin(rig: ReadRig) -> None:
    rig.archived = True
    result = owner.read_workspace(
        rig.db,
        query=EngineeringWorkspaceQuery(
            access=rig.access(REVOKE),
            project_id=rig.origin.project_id,
            task_id=rig.origin.task_id,
            technology=EngineeringTechnology.radio,
            purpose=EngineeringReadPurpose.revoke,
        ),
    )
    assert result.revision is None
    assert not result.readiness.is_ready


def test_archived_draft_explains_current_scope_blocker(rig: ReadRig) -> None:
    rig.archived = True
    result = owner.read_workspace(
        rig.db,
        query=EngineeringWorkspaceQuery(
            access=rig.access(WRITE),
            project_id=rig.origin.project_id,
            task_id=rig.origin.task_id,
            technology=EngineeringTechnology.fiber,
            purpose=EngineeringReadPurpose.draft,
        ),
    )
    assert not result.readiness.is_ready


def test_dispatch_without_approval_exposes_no_draft_or_history(rig: ReadRig) -> None:
    preview = owner.preview_dispatch(
        rig.db,
        query=ProjectDispatchPreviewQuery(
            access=rig.access("operations:dispatch:write"),
            project_id=rig.origin.project_id,
            task_id=rig.origin.task_id,
            technology=EngineeringTechnology.fiber,
        ),
    )
    assert preview.approval is None and not preview.readiness.is_ready
    assert rig.permissions == ["operations:dispatch:write"] * 2


def test_authorization_loss_after_locks_fails_closed(rig: ReadRig) -> None:
    rig.expire_after_wait = True
    with pytest.raises(owner.EngineeringPlanError, match="denied"):
        owner.preview_dispatch(
            rig.db,
            query=ProjectDispatchPreviewQuery(
                access=rig.access("operations:dispatch:write"),
                project_id=rig.origin.project_id,
                task_id=rig.origin.task_id,
                technology=EngineeringTechnology.fiber,
            ),
        )


def test_dispatch_cannot_request_engineering_history(rig: ReadRig) -> None:
    with pytest.raises(owner.EngineeringPlanError, match="denied"):
        owner.read_workspace(
            rig.db,
            query=EngineeringWorkspaceQuery(
                access=rig.access("operations:dispatch:write"),
                project_id=rig.origin.project_id,
                task_id=rig.origin.task_id,
                technology=EngineeringTechnology.fiber,
                purpose=EngineeringReadPurpose.overview,
            ),
        )


def seed_approval(rig: ReadRig) -> CompletionEngineeringPlanQuery:
    from decimal import Decimal

    from app.services.project_engineering_plan_contracts import (
        EngineeringCriterion,
        EngineeringPlanContent,
        GeoPoint,
        SurveyedPlanLocation,
    )

    plan, revision, approval, policy, actor, author = (uuid4() for _ in range(6))
    content = EngineeringPlanContent(
        technology=EngineeringTechnology.fiber,
        infrastructure=rig.origin.infrastructure,
        location=SurveyedPlanLocation(
            description="Site survey",
            point=GeoPoint(latitude=Decimal("9"), longitude=Decimal("7")),
        ),
        scope_statement="Approved native construction",
        criteria=(
            EngineeringCriterion(
                id=uuid4(),
                order=0,
                statement="Scope complete",
                verification_method="Inspect",
            ),
        ),
    )
    digest = owner._digest(content, rig.origin, None)
    rig.db.add_all(
        [
            EngineeringPolicyHead(
                project_id=rig.origin.project_id,
                generation=1,
                current_revision_id=policy,
            ),
            EngineeringPolicyRevision(
                id=policy,
                project_id=rig.origin.project_id,
                revision=1,
                independent_reviewer_required=False,
                actor_user_id=actor,
                actor_person_id=rig.person,
                reason="Explicit policy",
                command_id=uuid4(),
            ),
            ProjectEngineeringPlan(
                id=plan,
                project_id=rig.origin.project_id,
                task_id=rig.origin.task_id,
                technology="fiber",
                generation=2,
                latest_revision_id=revision,
                current_approval_id=approval,
            ),
            EngineeringPlanRevision(
                id=revision,
                plan_id=plan,
                revision=1,
                content=content.model_dump(mode="json", exclude={"criteria"}),
                origin_binding=owner._binding(rig.origin),
                digest=digest,
                author_user_id=actor,
                author_person_id=author,
                command_id=uuid4(),
            ),
            EngineeringPlanDecision(
                id=approval,
                plan_id=plan,
                project_id=rig.origin.project_id,
                revision_id=revision,
                policy_revision_id=policy,
                kind="approve",
                digest=digest,
                actor_user_id=actor,
                actor_person_id=rig.person,
                author_person_id=author,
                reason="Reviewed exact evidence",
                command_id=uuid4(),
            ),
            *(
                EngineeringPlanCriterion(
                    revision_id=revision,
                    id=item.id,
                    order=item.order,
                    statement=item.statement,
                    verification_method=item.verification_method,
                )
                for item in content.criteria
            ),
        ]
    )
    rig.db.flush()
    return CompletionEngineeringPlanQuery(
        access=rig.access("operations:completion-review:read"),
        plan_id=plan,
        project_id=rig.origin.project_id,
        task_id=rig.origin.task_id,
        expected_revision_id=revision,
        expected_approval_id=approval,
        expected_digest=digest,
    )


def test_completion_current_allows_nonissuable_status_but_dispatch_blocks(
    rig: ReadRig,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    rig.origin = replace(rig.origin, issuance_eligible=False)
    query = seed_approval(rig)

    def scope(db: Session, content: EngineeringPlanContent) -> UUID | None:
        return None

    monkeypatch.setattr(owner, "_scope_locks", scope)
    current = owner.resolve_current_for_completion(rig.db, query=query)
    assert current.approval_id == query.expected_approval_id
    preview = owner.preview_dispatch(
        rig.db,
        query=ProjectDispatchPreviewQuery(
            access=rig.access("operations:dispatch:write"),
            project_id=rig.origin.project_id,
            task_id=rig.origin.task_id,
            technology=EngineeringTechnology.fiber,
        ),
    )
    assert preview.approval is None and not preview.readiness.is_ready
    with pytest.raises(owner.EngineeringPlanError, match="unapproved"):
        owner.resolve_current(rig.db, query=replace(query, access=rig.access(READ)))


def test_completion_current_refuses_revoked_approval(rig: ReadRig) -> None:
    query = seed_approval(rig)
    head = rig.db.get(ProjectEngineeringPlan, query.plan_id)
    assert head is not None
    head.current_approval_id = None
    rig.db.flush()
    with pytest.raises(owner.EngineeringPlanError, match="unapproved"):
        owner.resolve_current_for_completion(rig.db, query=query)


def test_history_selected_revision_cannot_cross_plan_scope(rig: ReadRig) -> None:
    with pytest.raises(owner.EngineeringPlanError, match="not found"):
        owner.read_workspace(
            rig.db,
            query=EngineeringWorkspaceQuery(
                access=rig.access(READ),
                project_id=rig.origin.project_id,
                task_id=rig.origin.task_id,
                technology=EngineeringTechnology.fiber,
                purpose=EngineeringReadPurpose.overview,
                selected_revision_id=uuid4(),
            ),
        )


def test_history_reconstructs_ordered_native_criteria(rig: ReadRig) -> None:
    query = seed_approval(rig)
    result = owner.read_workspace(
        rig.db,
        query=EngineeringWorkspaceQuery(
            access=rig.access(READ),
            project_id=rig.origin.project_id,
            task_id=rig.origin.task_id,
            technology=EngineeringTechnology.fiber,
            purpose=EngineeringReadPurpose.overview,
            selected_revision_id=query.expected_revision_id,
        ),
    )
    assert result.revision is not None
    assert result.revision.content.criteria[0].statement == "Scope complete"
    assert result.history[0].content.criteria == result.revision.content.criteria


def test_historical_project_scope_keeps_canonical_labels_after_archival() -> None:
    from app.models.project import Project, ProjectInfrastructure, ProjectTask
    from app.services.projects import engineering_plan_revocation_scope

    engine = create_engine("sqlite://")
    Base.metadata.create_all(
        engine,
        tables=[
            Project.__table__,
            ProjectTask.__table__,
            ProjectInfrastructure.__table__,
        ],
    )
    with Session(engine) as db:
        project = Project(
            name="Northern ring build",
            number="PRJ-42",
            is_active=False,
            status="completed",
        )
        db.add(project)
        db.flush()
        task = ProjectTask(
            project_id=project.id,
            title="Cabinet installation",
            number="TASK-8",
            is_active=False,
            status="done",
            template_plan_state="superseded",
        )
        db.add(task)
        db.flush()
        scope = engineering_plan_revocation_scope(
            db, project_id=project.id, task_id=task.id
        )
        assert scope.project_label == "PRJ-42 · Northern ring build"
        assert scope.task_label == "TASK-8 · Cabinet installation"
        assert scope.project_id == project.id and scope.task_id == task.id
    engine.dispose()


def test_completion_current_refuses_changed_origin_binding(
    rig: ReadRig, monkeypatch: pytest.MonkeyPatch
) -> None:
    query = seed_approval(rig)
    rig.origin = replace(rig.origin, applied_template_revision=9)

    def scope(db: Session, content: EngineeringPlanContent) -> UUID | None:
        return None

    monkeypatch.setattr(owner, "_scope_locks", scope)
    with pytest.raises(owner.EngineeringPlanError, match="stale state"):
        owner.resolve_current_for_completion(rig.db, query=query)


@pytest.mark.parametrize(
    "field", ["expected_approval_id", "expected_revision_id", "expected_digest"]
)
def test_completion_current_refuses_changed_issued_identity(
    rig: ReadRig, field: str
) -> None:
    query = seed_approval(rig)
    changed = (
        replace(query, expected_approval_id=uuid4())
        if field == "expected_approval_id"
        else replace(query, expected_revision_id=uuid4())
        if field == "expected_revision_id"
        else replace(query, expected_digest="b" * 64)
    )
    with pytest.raises(owner.EngineeringPlanError, match="unapproved"):
        owner.resolve_current_for_completion(rig.db, query=changed)
