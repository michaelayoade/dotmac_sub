"""Migration682 acceptance on coordinator-owned dedicated PostgreSQL clones only."""

from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from threading import Barrier
from uuid import uuid4

import pytest
from sqlalchemy import Engine, func, select, text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.orm import Session

from app.models.field_completion_review import CompletionEnrollment
from app.models.project import Project, ProjectTask
from app.models.project_field_completion import (
    ProjectFieldCompletionApplication,
    ProjectFieldCompletionBasis,
    ProjectFieldCompletionLineage,
    ProjectFieldCompletionReceipt,
    ProjectFieldCompletionRequirement,
)
from app.models.rbac import Permission, SystemUserPermission
from app.schemas.project import ProjectTaskUpdate, ProjectUpdate
from app.services import projects
from app.services.domain_errors import DomainError
from app.services.field import completion_project_scope as owner
from app.services.field import completion_review
from app.services.field.completion_project_scope_contracts import (
    SCOPE,
    AcceptedProjectCompletionPreviewQuery,
    ApplyAcceptedProjectCompletion,
)
from app.services.field.completion_review_contracts import (
    WRITE,
    ReviewState,
    RevokeCompletionReview,
)
from app.services.field.execution_contracts import ApplyFieldTransition, FieldEvent
from app.services.field.transitions import field_transitions
from app.services.permission_admission import PermissionAdmission
from tests.integration.test_engineering_failed_scope_replacement_acceptance import (
    engineering_revisit,
)
from tests.integration.test_field_completion_review_acceptance import (
    Prepared,
    context,
    prepare,
    review,
)
from tests.integration.test_project_engineering_plan_acceptance import (
    NativeScope,
)
from tests.integration.test_project_engineering_plan_acceptance import (
    native_engine as engineering_native_engine,
)
from tests.integration.test_project_engineering_plan_acceptance import (
    scope as engineering_scope,
)

native_engine = engineering_native_engine
scope = engineering_scope


def staff_access(engine, user):
    from app.services import rbac_catalog
    from scripts.seed.seed_rbac import ADMIN_ONLY_PERMISSION_KEYS, DEFAULT_PERMISSIONS

    keys = {"project:update", "project:task:write"}
    with Session(engine) as db:
        # Migration-only clones do not run the runtime RBAC seed. Use its exact
        # catalogue declarations and flush-only owner before granting staff.
        for key, description in DEFAULT_PERMISSIONS:
            if key in keys:
                rbac_catalog.ensure_permission(
                    db,
                    key=key,
                    description=description,
                    is_ui_assignable=key not in ADMIN_ONLY_PERMISSION_KEYS,
                )
        permissions = db.scalars(
            select(Permission).where(
                Permission.key.in_(keys), Permission.is_active.is_(True)
            )
        ).all()
        assert {p.key for p in permissions} == keys
        for p in permissions:
            if (
                db.scalar(
                    select(SystemUserPermission.id).where(
                        SystemUserPermission.system_user_id == user,
                        SystemUserPermission.permission_id == p.id,
                    )
                )
                is None
            ):
                db.add(SystemUserPermission(system_user_id=user, permission_id=p.id))
        db.commit()
    return PermissionAdmission(user, "system_user", frozenset(keys))


def accepted(engine, scope):
    prepared = prepare(engine, scope)
    with Session(engine) as db:
        submitted = completion_review.submit_evidence(db, command=prepared.submission)
    with Session(engine) as db:
        result = completion_review.decide(db, command=review(prepared, submitted))
        marker = db.get(
            ProjectFieldCompletionRequirement, prepared.submission.enrollment_id
        )
        assert marker is not None, (
            "Actual enrollment must atomically register project requirement"
        )
    return prepared, result


def apply_command(engine, scope, user, *, task=False):
    access = staff_access(engine, user)
    with Session(engine) as db:
        preview = owner.preview_accepted_project_completion(
            db,
            query=AcceptedProjectCompletionPreviewQuery(
                access=access,
                project_id=scope.project_id,
                task_id=scope.task_id if task else None,
            ),
        )
    return ApplyAcceptedProjectCompletion(
        context=context(user, SCOPE),
        access=access,
        project_id=scope.project_id,
        task_id=scope.task_id if task else None,
        expected_project_revision=preview.project_revision,
        expected_task_revision=preview.task_revision,
        expected_requirement_token=preview.requirement_token,
    )


@pytest.mark.parametrize("task", [False, True])
def test_exact_accepted_project_or_task_completes_once_and_replays(
    native_engine: Engine, scope: NativeScope, task
):
    prepared, _ = accepted(native_engine, scope)
    command = apply_command(native_engine, scope, prepared.reviewer, task=task)
    with Session(native_engine) as db:
        result = owner.apply_accepted_project_completion(db, command=command)
    with Session(native_engine) as db:
        replay = owner.apply_accepted_project_completion(db, command=command)
        assert replay.application_id == result.application_id and replay.replayed
        assert (
            db.scalar(
                select(func.count()).select_from(ProjectFieldCompletionApplication)
            )
            == 1
        )
        assert (
            db.scalar(select(func.count()).select_from(ProjectFieldCompletionReceipt))
            == 1
        )
        basis = db.scalar(select(ProjectFieldCompletionBasis))
        assert basis.enrollment_id == prepared.submission.enrollment_id
        assert basis.effective_enrollment_id == basis.enrollment_id
        assert db.get(ProjectTask, scope.task_id).status == ("done" if task else "todo")
        assert db.get(Project, scope.project_id).status == (
            "active" if task else "completed"
        )


@pytest.mark.parametrize(
    "writer",
    [
        "project",
        "task",
        "mapped_project",
        "mapped_task",
        "raw_project",
        "raw_task",
        "archive_root",
        "archive_task",
    ],
)
def test_generic_and_raw_writers_cannot_complete_or_escape_enrolled_scope(
    native_engine: Engine, scope: NativeScope, writer
):
    prepare(native_engine, scope)
    with Session(native_engine) as db:
        with pytest.raises((DomainError, DBAPIError)):
            if writer == "project":
                projects.Projects.update(
                    db, str(scope.project_id), ProjectUpdate(status="completed")
                )
            elif writer == "task":
                projects.ProjectTasks.update(
                    db,
                    str(scope.task_id),
                    ProjectTaskUpdate(status="done"),
                    _status_transition=True,
                )
            elif writer == "mapped_project":
                db.get(Project, scope.project_id).status = "completed"
                db.flush()
            elif writer == "mapped_task":
                db.get(ProjectTask, scope.task_id).status = "done"
                db.flush()
            elif writer == "raw_project":
                db.execute(
                    text("UPDATE projects SET status='completed' WHERE id=:id"),
                    {"id": scope.project_id},
                )
            elif writer == "raw_task":
                db.execute(
                    text("UPDATE project_tasks SET status='done' WHERE id=:id"),
                    {"id": scope.task_id},
                )
            elif writer == "archive_root":
                db.execute(
                    text("UPDATE projects SET is_active=false WHERE id=:id"),
                    {"id": scope.project_id},
                )
            else:
                db.execute(
                    text("UPDATE project_tasks SET is_active=false WHERE id=:id"),
                    {"id": scope.task_id},
                )
        db.rollback()
        assert db.get(Project, scope.project_id).status == "active"
        assert db.get(ProjectTask, scope.task_id).status == "todo"


@pytest.mark.parametrize("stage", ["pending", "revoked"])
def test_pending_or_revoked_evidence_refuses_explicit_completion(
    native_engine: Engine, scope: NativeScope, stage
):
    if stage == "pending":
        prepared = prepare(native_engine, scope)
    else:
        prepared, result = accepted(native_engine, scope)
        revoke = RevokeCompletionReview(
            context=context(prepared.reviewer, WRITE),
            access=prepared.access,
            enrollment_id=result.enrollment_id,
            decision_id=result.decision_id,
            expected_generation=result.generation,
        )
        with Session(native_engine) as db:
            completion_review.revoke(db, command=revoke)
    command = apply_command(native_engine, scope, prepared.reviewer)
    with Session(native_engine) as db:
        with pytest.raises(DomainError):
            owner.apply_accepted_project_completion(db, command=command)
        assert db.scalar(select(ProjectFieldCompletionApplication.id)) is None


def test_replay_revalidates_revoked_current_basis(
    native_engine: Engine, scope: NativeScope
):
    prepared, result = accepted(native_engine, scope)
    command = apply_command(native_engine, scope, prepared.reviewer)
    with Session(native_engine) as db:
        owner.apply_accepted_project_completion(db, command=command)
    with Session(native_engine) as db:
        completion_review.revoke(
            db,
            command=RevokeCompletionReview(
                context=context(prepared.reviewer, WRITE),
                access=prepared.access,
                enrollment_id=result.enrollment_id,
                decision_id=result.decision_id,
                expected_generation=result.generation,
            ),
        )
    with Session(native_engine) as db:
        with pytest.raises(DomainError):
            owner.apply_accepted_project_completion(db, command=command)
        assert (
            db.get(Project, scope.project_id).status == "completed"
        )  # historical terminal unchanged


def test_stale_expected_revision_or_requirement_token_refuses(
    native_engine: Engine, scope: NativeScope
):
    prepared, _ = accepted(native_engine, scope)
    command = apply_command(native_engine, scope, prepared.reviewer)
    with Session(native_engine) as db:
        with pytest.raises(DomainError, match="changed|refresh"):
            owner.apply_accepted_project_completion(
                db, command=replace(command, expected_requirement_token="0" * 64)
            )
        assert db.scalar(select(ProjectFieldCompletionApplication.id)) is None


def test_duplicate_concurrent_intents_serialize_once(
    native_engine: Engine, scope: NativeScope
):
    prepared, _ = accepted(native_engine, scope)
    command = apply_command(native_engine, scope, prepared.reviewer)
    gate = Barrier(2)

    def run():
        with Session(native_engine) as db:
            gate.wait(timeout=10)
            return owner.apply_accepted_project_completion(db, command=command)

    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [pool.submit(run) for _ in range(2)]
        outcomes = [f.result(timeout=30) for f in futures]
    assert outcomes[0].application_id == outcomes[1].application_id
    assert sorted(r.replayed for r in outcomes) == [False, True]


def test_review_revocation_and_completion_serialize_without_deadlock(
    native_engine: Engine, scope: NativeScope
):
    prepared, result = accepted(native_engine, scope)
    command = apply_command(native_engine, scope, prepared.reviewer)
    revoke = RevokeCompletionReview(
        context=context(prepared.reviewer, WRITE),
        access=prepared.access,
        enrollment_id=result.enrollment_id,
        decision_id=result.decision_id,
        expected_generation=result.generation,
    )
    gate = Barrier(2)

    def complete():
        with Session(native_engine) as db:
            gate.wait(timeout=10)
            try:
                return owner.apply_accepted_project_completion(db, command=command)
            except DomainError:
                return None

    def withdraw():
        with Session(native_engine) as db:
            gate.wait(timeout=10)
            return completion_review.revoke(db, command=revoke)

    with ThreadPoolExecutor(max_workers=2) as pool:
        a, b = pool.submit(complete), pool.submit(withdraw)
        outcome = a.result(timeout=30)
        assert b.result(timeout=30).state == ReviewState.revoked
    with Session(native_engine) as db:
        assert db.get(Project, scope.project_id).status == (
            "completed" if outcome else "active"
        )
        with pytest.raises(DomainError):
            owner.apply_accepted_project_completion(db, command=command)


def test_outbox_or_audit_failure_rolls_back_application_and_status(
    native_engine: Engine, scope: NativeScope, monkeypatch
):
    prepared, _ = accepted(native_engine, scope)
    command = apply_command(native_engine, scope, prepared.reviewer)

    def fail(*args, **kwargs):
        raise RuntimeError("Injected application audit failure")

    monkeypatch.setattr(owner.audit_adapter, "stage", fail)
    with Session(native_engine) as db:
        with pytest.raises(RuntimeError, match="Injected"):
            owner.apply_accepted_project_completion(db, command=command)
        assert db.scalar(select(ProjectFieldCompletionApplication.id)) is None
        assert db.scalar(select(ProjectFieldCompletionBasis.application_id)) is None
        assert db.get(Project, scope.project_id).status == "active"


def test_engineering_failed_a_reviewed_b_accepted_b_closes_task_with_original_scope(
    native_engine: Engine, scope: NativeScope
):
    source, issued, enrolled, assignment, replacement, _ = engineering_revisit(
        native_engine, scope
    )
    with Session(native_engine) as db:
        edge = completion_review.review_failed_scope_replacement(
            db, command=replacement
        )
    for event in (FieldEvent.start, FieldEvent.complete):
        with Session(native_engine) as db:
            completed = field_transitions.apply(
                db,
                ApplyFieldTransition(
                    context=context(
                        source.submission.requester_system_user_id, "field:execution"
                    ),
                    requester_system_user_id=source.submission.requester_system_user_id,
                    public_id=issued.public_id,
                    event=event,
                    client_event_id=uuid4(),
                    assignment_generation=assignment.assignment_generation,
                ),
            )
    submission = replace(
        source.submission,
        context=context(
            source.submission.requester_system_user_id, "field:completion:submit"
        ),
        enrollment_id=enrolled.enrollment_id,
        criteria_revision_id=enrolled.criteria_revision_id,
        completed_event_id=completed.event.id,
        assignment_generation=assignment.assignment_generation,
    )
    with Session(native_engine) as db:
        submitted = completion_review.submit_evidence(db, command=submission)
    with Session(native_engine) as db:
        completion_review.decide(
            db,
            command=review(
                Prepared(submission, source.reviewer, source.access), submitted
            ),
        )
    command = apply_command(native_engine, scope, source.reviewer, task=True)
    with Session(native_engine) as db:
        result = owner.apply_accepted_project_completion(db, command=command)
    with Session(native_engine) as db:
        basis = db.get(
            ProjectFieldCompletionBasis,
            (result.application_id, source.submission.enrollment_id),
        )
        assert basis.effective_enrollment_id == enrolled.enrollment_id
        assert (
            db.get(
                ProjectFieldCompletionLineage,
                (result.application_id, source.submission.enrollment_id, 0),
            ).decision_id
            == edge.decision_id
        )
        assert db.get(ProjectTask, scope.task_id).status == "done"


def pending_successor_enrollment(engine, scope, prepared):
    """Issue a second real WO under the retained canonical approval; no fake proof."""
    from app.models.field_completion_review import CompletionEnrollment
    from app.models.project_engineering_plan import EngineeringPlanRevision
    from app.models.project_work_order_issuance import ProjectWorkOrderIssuance
    from app.services import project_work_order_handoff as handoff
    from app.services.field.completion_review_contracts import (
        CompletionPolicyScope,
        CompletionPolicyScopeKind,
        CompletionProvenance,
        EnrollCompletionReview,
    )
    from app.services.project_work_order_handoff_contracts import (
        PERMISSION,
        IssueProjectWorkOrder,
    )
    from app.services.work_order_templates import (
        ExplicitWorkOrderClassification,
        WorkOrderAccessTechnology,
        WorkOrderPurpose,
    )
    from tests.integration.test_engineering_failed_scope_replacement_acceptance import (
        stored_engineering_content,
    )

    with Session(engine) as db:
        original = db.get(CompletionEnrollment, prepared.submission.enrollment_id)
        receipt = db.get(ProjectWorkOrderIssuance, original.engineering_issuance_id)
        revision = db.get(EngineeringPlanRevision, receipt.revision_id)
        content = stored_engineering_content(db, revision)
        project = db.get(Project, scope.project_id)
        task = db.get(ProjectTask, scope.task_id)
        issue = IssueProjectWorkOrder(
            context=context(prepared.reviewer, PERMISSION),
            access=PermissionAdmission(
                prepared.reviewer, "system_user", frozenset({PERMISSION})
            ),
            project_id=receipt.project_id,
            task_id=receipt.task_id,
            expected_project_updated_at=project.updated_at,
            expected_task_updated_at=task.updated_at,
            plan_id=receipt.plan_id,
            revision_id=receipt.revision_id,
            approval_id=receipt.approval_id,
            digest=receipt.digest,
            infrastructure=content.infrastructure,
            location=content.location,
            classification=ExplicitWorkOrderClassification(
                purpose=WorkOrderPurpose.INFRASTRUCTURE_ROLLOUT,
                access_technology=WorkOrderAccessTechnology(receipt.technology),
            ),
            title="Separately issued exact physical requirement",
            description=None,
            priority="normal",
        )
    with Session(engine) as db:
        issued = handoff.issue(db, command=issue)
    return EnrollCompletionReview(
        context=context(prepared.reviewer, WRITE),
        access=prepared.access,
        work_order_public_id=issued.public_id,
        provenance=CompletionProvenance.engineering_issuance,
        engineering_issuance_id=issued.issuance_id,
        policy_scope=CompletionPolicyScope(
            kind=CompletionPolicyScopeKind.project, project_id=scope.project_id
        ),
        criteria=(),
    )


def test_enrollment_after_observation_refuses_missing_linkage_fence(
    native_engine: Engine, scope: NativeScope, monkeypatch
):
    from threading import Event

    prepared, _ = accepted(native_engine, scope)
    enrollment = pending_successor_enrollment(native_engine, scope, prepared)
    command = apply_command(native_engine, scope, prepared.reviewer)
    observed, release = Event(), Event()
    original = projects.project_field_completion_scope

    def pause(db, *, query):
        result = original(db, query=query)
        if not query.lock and db.info.get("pause_completion_observation"):
            observed.set()
            assert release.wait(10)
        return result

    monkeypatch.setattr(projects, "project_field_completion_scope", pause)

    def complete():
        with Session(native_engine) as db:
            db.info["pause_completion_observation"] = True
            with pytest.raises(DomainError, match="changed|refresh"):
                owner.apply_accepted_project_completion(db, command=command)

    with ThreadPoolExecutor(max_workers=2) as pool:
        future = pool.submit(complete)
        assert observed.wait(10)
        try:
            with Session(native_engine) as db:
                completion_review.enroll(db, command=enrollment)
        finally:
            release.set()
        future.result(timeout=30)
    with Session(native_engine) as db:
        assert (
            db.scalar(
                select(func.count()).select_from(ProjectFieldCompletionRequirement)
            )
            == 2
        )
        assert db.scalar(select(ProjectFieldCompletionApplication.id)) is None
        assert db.get(Project, scope.project_id).status == "active"


def test_raw_task_parent_update_fails_fast_against_project_scope_lock(
    native_engine: Engine, scope: NativeScope
):
    prepare(native_engine, scope)
    with Session(native_engine) as locked:
        locked.scalar(
            select(Project.id).where(Project.id == scope.project_id).with_for_update()
        )

        def raw():
            with Session(native_engine) as db:
                with pytest.raises(DBAPIError) as error:
                    db.execute(
                        text(
                            "UPDATE project_tasks SET parent_task_id=NULL WHERE id=:id"
                        ),
                        {"id": scope.task_id},
                    )
                assert (
                    error.value.orig.sqlstate == "55P03"
                )  # NOWAIT, no wait/deadlock inversion
                db.rollback()

        with ThreadPoolExecutor(max_workers=1) as pool:
            pool.submit(raw).result(timeout=5)
        locked.rollback()


@pytest.mark.parametrize(
    "table",
    [
        "project_field_completion_applications",
        "project_field_completion_bases",
        "project_field_completion_receipts",
    ],
)
def test_applied_evidence_sql_immutable(
    native_engine: Engine, scope: NativeScope, table
):
    prepared, _ = accepted(native_engine, scope)
    command = apply_command(native_engine, scope, prepared.reviewer)
    with Session(native_engine) as db:
        owner.apply_accepted_project_completion(db, command=command)
    with Session(native_engine) as db:
        with pytest.raises(DBAPIError, match="immutable"):
            db.execute(text(f"DELETE FROM {table}"))  # noqa: S608 - fixed parametrized table allowlist
        db.rollback()


def test_assignment_change_and_completion_have_one_serialized_current_basis(
    native_engine: Engine, scope: NativeScope
):
    from app.models.dispatch import TechnicianProfile
    from app.models.work_order import WorkOrder
    from app.services.work_order_assignment_contracts import (
        TechnicianAssignmentTarget,
        WorkOrderAssignmentCommand,
    )
    from app.services.work_order_commands import work_order_commands

    prepared, _ = accepted(native_engine, scope)
    command = apply_command(native_engine, scope, prepared.reviewer)
    with Session(native_engine) as db:
        enrolled = db.get(CompletionEnrollment, prepared.submission.enrollment_id)
        work = db.get(WorkOrder, enrolled.work_order_id)
        # A completed job cannot be reassigned through ordinary dispatch. A canonical
        # reopened execution state is required before assignment; this itself
        # invalidates acceptance and cannot race through the completion gate.
        work.status = "scheduled"
        db.commit()
        profile = db.scalar(
            select(TechnicianProfile).where(
                TechnicianProfile.system_user_id
                == prepared.submission.requester_system_user_id
            )
        )
        assignment = WorkOrderAssignmentCommand(
            access=prepared.access,
            work_order_public_id=work.public_id,
            target=TechnicianAssignmentTarget(profile.id),
            expected_revision=work.updated_at,
            expected_assignment_generation=prepared.submission.assignment_generation,
            reason="Revisit assignment changes exact execution generation",
        )
    gate = Barrier(2)

    def complete():
        with Session(native_engine) as db:
            gate.wait(10)
            with pytest.raises(DomainError):
                owner.apply_accepted_project_completion(db, command=command)

    def reassign():
        with Session(native_engine) as db:
            gate.wait(10)
            return work_order_commands.assign(
                db,
                command=assignment,
                context=context(prepared.reviewer, "operations:dispatch:assign"),
            )

    with ThreadPoolExecutor(max_workers=2) as pool:
        a, b = pool.submit(complete), pool.submit(reassign)
        a.result(30)
        assert (
            b.result(30).assignment_generation
            != prepared.submission.assignment_generation
        )
    with Session(native_engine) as db:
        assert db.scalar(select(ProjectFieldCompletionApplication.id)) is None


@pytest.mark.parametrize(
    "statement",
    [
        "ALTER TABLE project_field_completion_applications ALTER COLUMN reason DROP NOT NULL",
        "ALTER TABLE project_field_completion_applications ALTER COLUMN creation_outer_xid SET DEFAULT pg_current_xact_id()",
        "ALTER TABLE project_field_completion_applications ALTER COLUMN creation_outer_xid SET NOT NULL",
        "ALTER TABLE project_field_completion_applications ALTER COLUMN creation_outer_xid TYPE text",
        "CREATE INDEX project_field_unexpected_extra ON project_field_completion_requirements(registered_at)",
    ],
)
def test_frozen_empty_adoption_refuses_schema_drift(native_engine: Engine, statement):
    from scripts.migrations.project_completion_schema_contract import (
        verify_project_completion_adoption,
    )

    with native_engine.begin() as db:
        verify_project_completion_adoption(db)
        db.execute(text(statement))
        with pytest.raises(RuntimeError, match="schema mismatch"):
            verify_project_completion_adoption(db)


def test_adoption_refuses_nonempty_explicit_requirements(
    native_engine: Engine, scope: NativeScope
):
    from scripts.migrations.project_completion_schema_contract import (
        verify_project_completion_adoption,
    )

    prepare(native_engine, scope)
    with native_engine.begin() as db:
        with pytest.raises(RuntimeError, match="Nonempty"):
            verify_project_completion_adoption(db)


def test_sql_completion_refuses_application_receipt_without_full_basis(
    native_engine: Engine, scope: NativeScope
):
    from app.models.system_user import SystemUser

    prepared, _ = accepted(native_engine, scope)
    command = apply_command(native_engine, scope, prepared.reviewer)
    with Session(native_engine) as db:
        user = db.get(SystemUser, prepared.reviewer)
        app = ProjectFieldCompletionApplication(
            id=uuid4(),
            project_id=scope.project_id,
            task_id=None,
            expected_project_revision=command.expected_project_revision,
            expected_task_revision=None,
            requirement_token=command.expected_requirement_token,
            actor_user_id=user.id,
            actor_person_id=user.person_party_id,
            intent_id=uuid4(),
            command_id=uuid4(),
            reason="Attempt without accepted basis",
        )
        db.add(app)
        db.flush()
        db.add(
            ProjectFieldCompletionReceipt(
                intent_id=app.intent_id, application_id=app.id, fingerprint="a" * 64
            )
        )
        db.flush()
        with pytest.raises(DBAPIError, match="accepted field completion"):
            db.execute(
                text("UPDATE projects SET status='completed' WHERE id=:id"),
                {"id": scope.project_id},
            )
        db.rollback()
        assert db.scalar(select(ProjectFieldCompletionApplication.id)) is None


def test_frozen_adoption_rejects_missing_composite_receipt_binding(
    native_engine: Engine,
):
    from sqlalchemy import inspect

    from scripts.migrations.project_completion_schema_contract import (
        verify_project_completion_adoption,
    )

    with native_engine.begin() as db:
        verify_project_completion_adoption(db)
        foreign = next(
            f
            for f in inspect(db).get_foreign_keys("project_field_completion_receipts")
            if tuple(f["constrained_columns"]) == ("application_id", "intent_id")
        )
        name = foreign["name"].replace('"', '""')
        db.execute(
            text(
                f'ALTER TABLE project_field_completion_receipts DROP CONSTRAINT "{name}"'
            )
        )  # noqa: S608 - introspected identifier only
        with pytest.raises(RuntimeError, match="schema mismatch"):
            verify_project_completion_adoption(db)


def reopen_for_event(engine, scope, *, enrollment_id, actor, generation, event):
    """Simulate repair reopening; append the event through canonical execution."""
    from app.models.work_order import WorkOrder
    from app.services.field.execution_contracts import FieldTransitionPayload

    with Session(engine) as db:
        enrollment = db.get(CompletionEnrollment, enrollment_id)
        work = db.get(WorkOrder, enrollment.work_order_id)
        work.status = "in_progress"
        public_id = work.public_id
        db.commit()
    with Session(engine) as db:
        return field_transitions.apply(
            db,
            ApplyFieldTransition(
                context=context(actor, "field:execution"),
                requester_system_user_id=actor,
                public_id=public_id,
                event=event,
                client_event_id=uuid4(),
                assignment_generation=generation,
                payload=FieldTransitionPayload(reason="no_access")
                if event == FieldEvent.unable_to_complete
                else FieldTransitionPayload(),
            ),
        )


def replacement_application_for_event_guard(engine, scope, *, unable):
    from app.services.field.completion_review_contracts import UnableToCompleteFailure

    source, issued, enrolled, assignment, replacement, _ = engineering_revisit(
        engine, scope
    )
    if unable:
        failure = reopen_for_event(
            engine,
            scope,
            enrollment_id=source.submission.enrollment_id,
            actor=source.submission.requester_system_user_id,
            generation=source.submission.assignment_generation,
            event=FieldEvent.unable_to_complete,
        )
        replacement = replace(
            replacement,
            failure_basis=UnableToCompleteFailure(
                failure.event.id, source.submission.assignment_generation
            ),
        )
    with Session(engine) as db:
        completion_review.review_failed_scope_replacement(db, command=replacement)
    for event in (FieldEvent.start, FieldEvent.complete):
        with Session(engine) as db:
            completed = field_transitions.apply(
                db,
                ApplyFieldTransition(
                    context=context(
                        source.submission.requester_system_user_id, "field:execution"
                    ),
                    requester_system_user_id=source.submission.requester_system_user_id,
                    public_id=issued.public_id,
                    event=event,
                    client_event_id=uuid4(),
                    assignment_generation=assignment.assignment_generation,
                ),
            )
    submission = replace(
        source.submission,
        context=context(
            source.submission.requester_system_user_id, "field:completion:submit"
        ),
        enrollment_id=enrolled.enrollment_id,
        criteria_revision_id=enrolled.criteria_revision_id,
        completed_event_id=completed.event.id,
        assignment_generation=assignment.assignment_generation,
    )
    with Session(engine) as db:
        submitted = completion_review.submit_evidence(db, command=submission)
    with Session(engine) as db:
        completion_review.decide(
            db,
            command=review(
                Prepared(submission, source.reviewer, source.access), submitted
            ),
        )
    command = apply_command(engine, scope, source.reviewer)
    with Session(engine) as db:
        application = owner.apply_accepted_project_completion(db, command=command)
    return source, application


def assert_historical_basis_cannot_complete_again(engine, scope, application):
    """Exercise raw application/bases/receipt/status, bypassing the Python proof."""
    from sqlalchemy import inspect

    with Session(engine) as db:
        original = db.get(ProjectFieldCompletionApplication, application.application_id)
        values = {
            c.key: getattr(original, c.key)
            for c in inspect(ProjectFieldCompletionApplication).columns
            if c.key
            not in {
                "id",
                "intent_id",
                "command_id",
                "applied_at",
                "expected_project_revision",
            }
        }
        values["expected_project_revision"] = db.get(
            Project, scope.project_id
        ).updated_at
        fresh = ProjectFieldCompletionApplication(
            id=uuid4(), intent_id=uuid4(), command_id=uuid4(), **values
        )
        db.add(fresh)
        db.flush()
        for basis in db.scalars(
            select(ProjectFieldCompletionBasis).where(
                ProjectFieldCompletionBasis.application_id == original.id
            )
        ).all():
            fields = {
                c.key: getattr(basis, c.key)
                for c in inspect(ProjectFieldCompletionBasis).columns
                if c.key != "application_id"
            }
            db.add(ProjectFieldCompletionBasis(application_id=fresh.id, **fields))
        db.flush()
        for edge in db.scalars(
            select(ProjectFieldCompletionLineage).where(
                ProjectFieldCompletionLineage.application_id == original.id
            )
        ).all():
            db.add(
                ProjectFieldCompletionLineage(
                    application_id=fresh.id,
                    enrollment_id=edge.enrollment_id,
                    position=edge.position,
                    decision_id=edge.decision_id,
                )
            )
        db.add(
            ProjectFieldCompletionReceipt(
                application_id=fresh.id, intent_id=fresh.intent_id, fingerprint="c" * 64
            )
        )
        db.flush()
        with pytest.raises(DBAPIError, match="evidence mismatch|lineage mismatch"):
            db.execute(
                text("UPDATE projects SET status='completed' WHERE id=:id"),
                {"id": scope.project_id},
            )
            db.execute(text("SET CONSTRAINTS ALL IMMEDIATE"))
        db.rollback()
        assert db.get(Project, scope.project_id).status == "active"
        assert (
            db.scalar(
                select(func.count()).select_from(ProjectFieldCompletionApplication)
            )
            == 1
        )


@pytest.mark.parametrize(
    "drift",
    [
        "unable_status",
        "unable_new_event",
        "rejected_new_complete",
        "effective_new_complete",
    ],
)
def test_sql_basis_requires_latest_authoritative_failure_and_completion_events(
    native_engine: Engine, scope: NativeScope, drift
):
    from app.models.work_order import WorkOrder

    if drift == "effective_new_complete":
        prepared, _ = accepted(native_engine, scope)
        command = apply_command(native_engine, scope, prepared.reviewer)
        with Session(native_engine) as db:
            application = owner.apply_accepted_project_completion(db, command=command)
    else:
        prepared, application = replacement_application_for_event_guard(
            native_engine, scope, unable=drift.startswith("unable")
        )
    # Existing immutable application is historical. A direct repair reopening
    # permits exercising SQL against a fresh forged attempt using its old facts.
    with Session(native_engine) as db:
        db.execute(
            text("UPDATE projects SET status='active' WHERE id=:id"),
            {"id": scope.project_id},
        )
        db.commit()
    if drift == "unable_status":
        with Session(native_engine) as db:
            enrollment = db.get(CompletionEnrollment, prepared.submission.enrollment_id)
            db.get(WorkOrder, enrollment.work_order_id).status = "scheduled"
            db.commit()
    else:
        reopen_for_event(
            native_engine,
            scope,
            enrollment_id=prepared.submission.enrollment_id,
            actor=prepared.submission.requester_system_user_id,
            generation=prepared.submission.assignment_generation,
            event=FieldEvent.unable_to_complete
            if drift == "unable_new_event"
            else FieldEvent.complete,
        )
    assert_historical_basis_cannot_complete_again(native_engine, scope, application)


@pytest.mark.parametrize("task", [False, True])
@pytest.mark.parametrize("immediate", [False, True])
def test_canonical_completion_in_external_connection_savepoint_commits(
    native_engine: Engine, scope: NativeScope, task, immediate
):
    """The executor's real external-Connection savepoint retains outer identity."""
    prepared, _ = accepted(native_engine, scope)
    command = apply_command(native_engine, scope, prepared.reviewer, task=task)
    with native_engine.connect() as connection:
        transaction = connection.begin()
        with Session(connection, join_transaction_mode="create_savepoint") as db:
            result = owner.apply_accepted_project_completion(db, command=command)
        marker = connection.scalar(
            text(
                "SELECT creation_outer_xid::text FROM project_field_completion_applications WHERE id=:id"
            ),
            {"id": result.application_id},
        )
        assert marker == connection.scalar(text("SELECT pg_current_xact_id()::text"))
        if immediate:
            connection.execute(text("SET CONSTRAINTS ALL IMMEDIATE"))
        transaction.commit()  # Exercise the actual physical COMMIT, not just RELEASE.
    with Session(native_engine) as db:
        assert (
            db.get(
                ProjectFieldCompletionApplication, result.application_id
            ).creation_outer_xid
            == marker
        )
        assert db.get(ProjectTask, scope.task_id).status == ("done" if task else "todo")
        assert db.get(Project, scope.project_id).status == (
            "active" if task else "completed"
        )


def raw_application_values(engine, scope, prepared):
    """Use exact real scope/actor/revision values for SQL provenance canaries."""
    from app.models.system_user import SystemUser

    command = apply_command(engine, scope, prepared.reviewer)
    with Session(engine) as db:
        person_id = db.get(SystemUser, prepared.reviewer).person_party_id
    return {
        "id": uuid4(),
        "project_id": scope.project_id,
        "task_id": None,
        "expected_project_revision": command.expected_project_revision,
        "expected_task_revision": None,
        "requirement_token": command.expected_requirement_token,
        "actor_user_id": prepared.reviewer,
        "actor_person_id": person_id,
        "intent_id": uuid4(),
        "command_id": uuid4(),
        "reason": "Outer transaction provenance canary",
    }


@pytest.mark.parametrize("layout", ["nested", "released", "sibling", "rollback_retry"])
def test_application_children_share_outer_identity_across_real_savepoints(
    native_engine: Engine, scope: NativeScope, layout
):
    prepared, _ = accepted(native_engine, scope)
    values = raw_application_values(native_engine, scope, prepared)
    with native_engine.connect() as connection:
        outer = connection.begin()
        parent = connection.begin_nested()
        connection.execute(
            ProjectFieldCompletionApplication.__table__.insert().values(**values)
        )
        if layout == "rollback_retry":
            parent.rollback()
            assert (
                connection.scalar(
                    select(ProjectFieldCompletionApplication.id).where(
                        ProjectFieldCompletionApplication.id == values["id"]
                    )
                )
                is None
            )
            parent = connection.begin_nested()
            connection.execute(
                ProjectFieldCompletionApplication.__table__.insert().values(**values)
            )
        if layout in {"released", "sibling", "rollback_retry"}:
            parent.commit()
        child = (
            connection.begin_nested()
            if layout in {"nested", "sibling", "rollback_retry"}
            else None
        )
        connection.execute(
            ProjectFieldCompletionReceipt.__table__.insert().values(
                intent_id=values["intent_id"],
                application_id=values["id"],
                fingerprint="a" * 64,
            )
        )
        if child is not None:
            child.commit()
        if parent.is_active:
            parent.commit()
        assert (
            connection.scalar(
                text(
                    "SELECT creation_outer_xid=pg_current_xact_id() FROM project_field_completion_applications WHERE id=:id"
                ),
                {"id": values["id"]},
            )
            is True
        )
        outer.commit()
    with Session(native_engine) as db:
        assert (
            db.get(ProjectFieldCompletionReceipt, values["intent_id"]).application_id
            == values["id"]
        )


@pytest.mark.parametrize("child", ["receipt", "basis", "lineage"])
def test_application_from_previous_physical_transaction_refuses_late_children(
    native_engine: Engine, scope: NativeScope, child
):
    prepared, _ = accepted(native_engine, scope)
    values = raw_application_values(native_engine, scope, prepared)
    with native_engine.begin() as connection:
        connection.execute(
            ProjectFieldCompletionApplication.__table__.insert().values(**values)
        )
    with native_engine.begin() as connection:
        with pytest.raises(
            DBAPIError, match="must accompany this transaction application"
        ):
            if child == "receipt":
                connection.execute(
                    ProjectFieldCompletionReceipt.__table__.insert().values(
                        intent_id=values["intent_id"],
                        application_id=values["id"],
                        fingerprint="b" * 64,
                    )
                )
            elif child == "lineage":
                # BEFORE INSERT must refuse provenance before any missing FK is considered.
                connection.execute(
                    ProjectFieldCompletionLineage.__table__.insert().values(
                        application_id=values["id"],
                        enrollment_id=prepared.submission.enrollment_id,
                        position=0,
                        decision_id=uuid4(),
                    )
                )
            else:
                connection.execute(
                    text(
                        "INSERT INTO project_field_completion_bases(application_id,enrollment_id) VALUES (:app,:enrollment)"
                    ),
                    {
                        "app": values["id"],
                        "enrollment": prepared.submission.enrollment_id,
                    },
                )
        connection.rollback()
    with Session(native_engine) as db:
        assert db.get(ProjectFieldCompletionApplication, values["id"]) is not None
        assert db.get(ProjectFieldCompletionReceipt, values["intent_id"]) is None


@pytest.mark.parametrize("supplied", [None, "1", "18446744073709551615"])
def test_application_outer_xid_is_server_stamped_and_cannot_be_updated(
    native_engine: Engine, scope: NativeScope, supplied
):
    prepared, _ = accepted(native_engine, scope)
    values = raw_application_values(native_engine, scope, prepared)
    with native_engine.begin() as connection:
        connection.execute(
            ProjectFieldCompletionApplication.__table__.insert().values(
                **values, creation_outer_xid=supplied
            )
        )
        marker = connection.scalar(
            text(
                "SELECT creation_outer_xid::text FROM project_field_completion_applications WHERE id=:id"
            ),
            {"id": values["id"]},
        )
        assert marker == connection.scalar(text("SELECT pg_current_xact_id()::text"))
    with native_engine.begin() as connection:
        with pytest.raises(DBAPIError, match="immutable|outer transaction"):
            connection.execute(
                text(
                    "UPDATE project_field_completion_applications SET creation_outer_xid=pg_current_xact_id() WHERE id=:id"
                ),
                {"id": values["id"]},
            )
        connection.rollback()
