"""Real issuance/rejected visit/reviewed engineering replacement on migrated PG."""

from dataclasses import replace
from uuid import uuid4

import pytest
from sqlalchemy import Engine, select
from sqlalchemy.orm import Session

from app.models.dispatch import TechnicianProfile
from app.models.field_completion_review import CompletionEnrollment
from app.models.project import Project, ProjectStatus, ProjectTask
from app.models.project_engineering_plan import (
    EngineeringPlanCriterion,
    EngineeringPlanRevision,
)
from app.models.project_work_order_issuance import ProjectWorkOrderIssuance
from app.models.work_order import WorkOrder
from app.services import project_work_order_handoff as handoff
from app.services.domain_errors import DomainError
from app.services.field import completion_review as owner
from app.services.field.completion_review_contracts import (
    WRITE,
    CompletionPolicyScope,
    CompletionPolicyScopeKind,
    CompletionProvenance,
    CriterionCoverage,
    CriterionVerdict,
    EnrollCompletionReview,
    FailedScopeReplacementWorkspaceQuery,
    RejectedSubmissionFailure,
    ReplacementWorkspacePurpose,
    ReviewFailedScopeReplacement,
    ReviewState,
    RevokeFailedScopeReplacement,
)
from app.services.field.execution_contracts import ApplyFieldTransition, FieldEvent
from app.services.field.transitions import field_transitions
from app.services.permission_admission import PermissionAdmission
from app.services.project_engineering_plan_contracts import (
    EngineeringCriterion,
    EngineeringPlanContent,
)
from app.services.project_work_order_handoff_contracts import (
    PERMISSION,
    IssueProjectWorkOrder,
)
from app.services.work_order_assignment_contracts import (
    TechnicianAssignmentTarget,
    WorkOrderAssignmentCommand,
)
from app.services.work_order_commands import work_order_commands
from app.services.work_order_templates import (
    ExplicitWorkOrderClassification,
    WorkOrderAccessTechnology,
    WorkOrderPurpose,
)
from tests.integration.test_field_completion_review_acceptance import (
    Prepared,
    context,
    prepare,
    review,
)
from tests.integration.test_project_engineering_plan_acceptance import NativeScope
from tests.integration.test_project_engineering_plan_acceptance import (
    context as engineering_context,
)
from tests.integration.test_project_engineering_plan_acceptance import (
    native_engine as engineering_native_engine,
)
from tests.integration.test_project_engineering_plan_acceptance import (
    scope as engineering_scope,
)

native_engine = engineering_native_engine
scope = engineering_scope


def stored_engineering_content(
    db: Session, revision: EngineeringPlanRevision
) -> EngineeringPlanContent:
    """Production stores revision content and immutable canonical criteria separately."""
    criteria = tuple(
        EngineeringCriterion(
            id=row.id,
            order=row.order,
            statement=row.statement,
            verification_method=row.verification_method,
        )
        for row in db.scalars(
            select(EngineeringPlanCriterion)
            .where(EngineeringPlanCriterion.revision_id == revision.id)
            .order_by(EngineeringPlanCriterion.order)
        ).all()
    )
    return EngineeringPlanContent.model_validate(
        {**revision.content, "criteria": criteria}
    )


def engineering_revisit(engine: Engine, scope: NativeScope, *, already_issued=None):
    source = prepare(engine, scope, already_issued=already_issued)
    with Session(engine) as db:
        submitted = owner.submit_evidence(db, command=source.submission)
    accepted = review(source, submitted)
    rejected_command = replace(
        accepted,
        results=tuple(
            replace(item, verdict=CriterionVerdict.failed) for item in accepted.results
        ),
    )
    with Session(engine) as db:
        rejected = owner.decide(db, command=rejected_command)
    with Session(engine) as db:
        enrollment = db.get(CompletionEnrollment, source.submission.enrollment_id)
        assert enrollment is not None
        receipt = db.get(ProjectWorkOrderIssuance, enrollment.engineering_issuance_id)
        assert receipt is not None
        revision = db.get(EngineeringPlanRevision, receipt.revision_id)
        assert revision is not None
        content = stored_engineering_content(db, revision)
        project = db.get(Project, scope.project_id)
        task = db.get(ProjectTask, scope.task_id)
        assert project and task
        issue = IssueProjectWorkOrder(
            context=context(scope.reviewer_id, PERMISSION),
            access=PermissionAdmission(
                scope.reviewer_id, "system_user", frozenset({PERMISSION})
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
            title="Explicit engineering revisit",
            description=None,
            priority="normal",
        )
        technician = db.scalar(
            select(TechnicianProfile).where(
                TechnicianProfile.system_user_id
                == source.submission.requester_system_user_id
            )
        )
        assert technician is not None
        profile_id = technician.id
    with Session(engine) as db:
        issued = handoff.issue(db, command=issue)
    with Session(engine) as db:
        row = db.get(WorkOrder, issued.work_order_id)
        assert row is not None
        token = row.updated_at
    with Session(engine) as db:
        assignment = work_order_commands.assign(
            db,
            command=WorkOrderAssignmentCommand(
                access=source.access,
                work_order_public_id=issued.public_id,
                target=TechnicianAssignmentTarget(profile_id),
                expected_revision=token,
                reason="Assign explicit engineering revisit",
            ),
            context=context(scope.reviewer_id, "operations:dispatch:assign"),
        )
    assert assignment.assignment_generation is not None
    with Session(engine) as db:
        enrolled = owner.enroll(
            db,
            command=EnrollCompletionReview(
                context=context(scope.reviewer_id, WRITE),
                access=source.access,
                work_order_public_id=issued.public_id,
                provenance=CompletionProvenance.engineering_issuance,
                engineering_issuance_id=issued.issuance_id,
                policy_scope=CompletionPolicyScope(
                    kind=CompletionPolicyScopeKind.project, project_id=scope.project_id
                ),
                criteria=(),
            ),
        )
    assert (
        enrolled.enrollment_id
        and enrolled.criteria_revision_id
        and rejected.decision_id
    )
    command = ReviewFailedScopeReplacement(
        context=context(scope.reviewer_id, WRITE),
        access=source.access,
        source_enrollment_id=source.submission.enrollment_id,
        replacement_enrollment_id=enrolled.enrollment_id,
        expected_generation=0,
        failure_basis=RejectedSubmissionFailure(
            submitted.submission_id, rejected.decision_id, rejected.generation
        ),
        source_criteria_revision_id=source.submission.criteria_revision_id,
        replacement_criteria_revision_id=enrolled.criteria_revision_id,
        source_policy_revision_id=source.submission.policy_revision_id,
        replacement_policy_revision_id=source.submission.policy_revision_id,
        source_assignment_generation=source.submission.assignment_generation,
        replacement_assignment_generation=assignment.assignment_generation,
        coverage=tuple(
            CriterionCoverage(item.criterion_id, item.criterion_id)
            for item in source.submission.observations
        ),
    )
    return source, issued, enrolled, assignment, command, issue


def test_engineering_revisit_requires_own_accepted_completion(
    native_engine: Engine, scope: NativeScope
):
    source, issued, enrolled, assignment, command, _ = engineering_revisit(
        native_engine, scope
    )
    with Session(native_engine) as db:
        approved = owner.review_failed_scope_replacement(db, command=command)
    with Session(native_engine) as db:
        workspace = owner.failed_scope_replacement_workspace(
            db,
            query=FailedScopeReplacementWorkspaceQuery(
                access=source.access,
                work_order_public_id=issued.public_id,
                purpose=ReplacementWorkspacePurpose.read,
            ),
        )
        assert workspace.source.current_review_state != ReviewState.accepted
        assert approved.decision_id is not None
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
    replacement = replace(
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
        submitted = owner.submit_evidence(db, command=replacement)
    prepared = Prepared(replacement, source.reviewer, source.access)
    with Session(native_engine) as db:
        accepted = owner.decide(db, command=review(prepared, submitted))
    assert accepted.state == ReviewState.accepted


def test_existing_engineering_revisit_review_on_hold_but_new_issuance_blocked(
    native_engine: Engine, scope: NativeScope
):
    _, _, _, _, command, issue = engineering_revisit(native_engine, scope)
    with Session(native_engine) as db:
        project = db.get(Project, scope.project_id)
        assert project is not None
        project.status = ProjectStatus.on_hold
        db.commit()
    with Session(native_engine) as db:
        approved = owner.review_failed_scope_replacement(db, command=command)
    with Session(native_engine) as db:
        with pytest.raises(DomainError):
            handoff.issue(
                db,
                command=replace(issue, context=context(scope.reviewer_id, PERMISSION)),
            )
    with Session(native_engine) as db:
        revoked = owner.revoke_failed_scope_replacement(
            db,
            command=RevokeFailedScopeReplacement(
                context=context(scope.reviewer_id, WRITE),
                access=command.access,
                source_enrollment_id=command.source_enrollment_id,
                decision_id=approved.decision_id,
                expected_generation=approved.generation,
            ),
        )
    assert revoked.generation == approved.generation + 1


def test_engineering_coverage_must_preserve_canonical_criterion_identity(
    native_engine: Engine, scope: NativeScope
):
    _, _, _, _, command, _ = engineering_revisit(native_engine, scope)
    swapped = tuple(
        CriterionCoverage(item.source_criterion_id, uuid4())
        for item in command.coverage
    )
    with Session(native_engine) as db:
        with pytest.raises(DomainError):
            owner.review_failed_scope_replacement(
                db, command=replace(command, coverage=swapped)
            )


def radio_issued(engine: Engine, scope: NativeScope):
    from app.models.network_monitoring import PopSite
    from app.models.project import ProjectInfrastructure
    from app.models.rbac import Permission, SystemUserPermission
    from app.schemas.infrastructure import InfrastructureReference, InfrastructureType
    from app.services import project_engineering_plans as plans
    from app.services.project_engineering_plan_contracts import EngineeringTechnology
    from tests.integration.test_project_engineering_plan_acceptance import (
        prepare as prepare_plan,
    )
    from tests.integration.test_project_engineering_plan_acceptance import (
        review as review_plan,
    )

    configured, draft, _ = prepare_plan(engine, scope)
    with Session(engine) as db:
        base = PopSite(
            name="Native radio engineering target",
            # Native BTS catalogue membership, confined to this synthetic clone.
            zabbix_group_id=str(uuid4().int)[:20],
            latitude=9.1,
            longitude=7.2,
            is_active=True,
        )
        db.add(base)
        db.flush()
        target = db.get(ProjectInfrastructure, scope.project_id)
        assert target is not None
        target.pon_port_id = None
        target.base_station_id = base.id
        reference = InfrastructureReference(
            type=InfrastructureType.base_station, id=base.id
        )
        from app.services import infrastructure_catalogue

        assert (
            infrastructure_catalogue.resolve(db, reference=reference, active_only=True)
            is not None
        )
        permission = db.scalar(
            select(Permission).where(
                Permission.key == PERMISSION, Permission.is_active.is_(True)
            )
        )
        assert permission is not None
        db.add(
            SystemUserPermission(
                system_user_id=scope.reviewer_id, permission_id=permission.id
            )
        )
        db.commit()
    radio_draft = replace(
        draft,
        context=engineering_context(scope.author_id),
        plan_id=uuid4(),
        content=draft.content.model_copy(
            update={
                "technology": EngineeringTechnology.radio,
                "infrastructure": reference,
            }
        ),
    )
    with Session(engine) as db:
        revision = plans.draft_revision(db, command=radio_draft)
    with Session(engine) as db:
        approved = plans.decide(db, command=review_plan(scope, configured, revision))
    command = IssueProjectWorkOrder(
        context=context(scope.reviewer_id, PERMISSION),
        access=PermissionAdmission(
            scope.reviewer_id, "system_user", frozenset({PERMISSION})
        ),
        project_id=scope.project_id,
        task_id=scope.task_id,
        expected_project_updated_at=scope.project_token,
        expected_task_updated_at=scope.task_token,
        plan_id=approved.plan_id,
        revision_id=approved.revision_id,
        approval_id=approved.decision_id,
        digest=approved.digest,
        infrastructure=reference,
        location=radio_draft.content.location,
        classification=ExplicitWorkOrderClassification(
            purpose=WorkOrderPurpose.INFRASTRUCTURE_ROLLOUT,
            access_technology=WorkOrderAccessTechnology.RADIO,
        ),
        title="Native radio completion source",
        description=None,
        priority="normal",
    )
    with Session(engine) as db:
        return handoff.issue(db, command=command)


def test_native_radio_exact_equivalent_reviewed_revisit(
    native_engine: Engine, scope: NativeScope
):
    issued = radio_issued(native_engine, scope)
    _, replacement, _, _, command, _ = engineering_revisit(
        native_engine, scope, already_issued=issued
    )
    with Session(native_engine) as db:
        result = owner.review_failed_scope_replacement(db, command=command)
    assert result.replacement_enrollment_id == command.replacement_enrollment_id
    with Session(native_engine) as db:
        row = db.get(WorkOrder, replacement.work_order_id)
        assert row is not None and row.access_technology == "radio"


@pytest.mark.parametrize("drift", ["target", "approval"])
def test_engineering_current_proof_refuses_scope_or_approval_drift_but_history_revoke_survives(
    native_engine: Engine, scope: NativeScope, drift
):
    from app.models.project_engineering_plan import (
        EngineeringPolicyHead,
        ProjectEngineeringPlan,
    )
    from app.services import project_engineering_plans as plans
    from app.services.field.completion_review_contracts import (
        CurrentReplacementEvidenceQuery,
    )
    from app.services.project_engineering_plan_contracts import (
        DecideEngineeringPlan,
        PlanDecisionKind,
    )
    from tests.integration.test_project_engineering_plan_acceptance import admission

    _, issued, _, _, command, issue = engineering_revisit(native_engine, scope)
    with Session(native_engine) as db:
        approved = owner.review_failed_scope_replacement(db, command=command)
    if drift == "target":
        with Session(native_engine) as db:
            row = db.get(WorkOrder, issued.work_order_id)
            assert row is not None
            row.purpose = "repair"
            db.commit()
    else:
        with Session(native_engine) as db:
            head = db.get(ProjectEngineeringPlan, issue.plan_id)
            policy = db.get(EngineeringPolicyHead, scope.project_id)
            assert head and policy
            revoke = DecideEngineeringPlan(
                context=engineering_context(scope.reviewer_id),
                access=admission(scope.reviewer_id),
                project_id=scope.project_id,
                task_id=scope.task_id,
                expected_project_updated_at=scope.project_token,
                expected_task_updated_at=scope.task_token,
                plan_id=issue.plan_id,
                revision_id=issue.revision_id,
                expected_digest=issue.digest,
                expected_policy_revision_id=policy.current_revision_id,
                expected_generation=head.generation,
                decision=PlanDecisionKind.revoke,
            )
        with Session(native_engine) as db:
            plans.decide(db, command=revoke)
    with Session(native_engine) as db:
        source = db.get(CompletionEnrollment, command.source_enrollment_id)
        assert source is not None
        with pytest.raises(DomainError):
            owner.current_replacement_evidence(
                db,
                query=CurrentReplacementEvidenceQuery(
                    access=command.access,
                    source_work_order_id=source.work_order_id,
                    decision_id=approved.decision_id,
                    failure_basis=command.failure_basis,
                ),
            )
    with Session(native_engine) as db:
        revoked = owner.revoke_failed_scope_replacement(
            db,
            command=RevokeFailedScopeReplacement(
                context=context(scope.reviewer_id, WRITE),
                access=command.access,
                source_enrollment_id=command.source_enrollment_id,
                decision_id=approved.decision_id,
                expected_generation=approved.generation,
            ),
        )
    assert revoked.generation == approved.generation + 1


def test_sql_engineering_decision_lineage_refuses_noncanonical_current_work(
    native_engine: Engine, scope: NativeScope
):
    from sqlalchemy import insert, literal
    from sqlalchemy.exc import DBAPIError

    from app.models.field_completion_review import CompletionReplacementDecision

    _, issued, _, _, command, _ = engineering_revisit(native_engine, scope)
    with Session(native_engine) as db:
        approved = owner.review_failed_scope_replacement(db, command=command)
    with Session(native_engine) as db:
        row = db.get(WorkOrder, issued.work_order_id)
        assert row is not None
        row.purpose = "repair"
        db.commit()
    table = CompletionReplacementDecision.__table__
    columns = tuple(table.columns)
    values = [
        literal(uuid4())
        if column.name == "id"
        else literal(2)
        if column.name == "generation"
        else column
        for column in columns
    ]
    with Session(native_engine) as db:
        with pytest.raises(
            DBAPIError, match="current engineering approval and canonical criteria"
        ):
            db.execute(
                insert(table).from_select(
                    [column.name for column in columns],
                    select(*values).where(table.c.id == approved.decision_id),
                )
            )
            db.commit()


@pytest.mark.parametrize("retained_change", ["later_draft", "later_policy"])
def test_retained_current_engineering_approval_still_allows_exact_replacement(
    native_engine: Engine, scope: NativeScope, retained_change
):
    from app.models.project_engineering_plan import (
        EngineeringPolicyHead,
        ProjectEngineeringPlan,
    )
    from app.services import project_engineering_plans as plans
    from app.services.project_engineering_plan_contracts import (
        DraftEngineeringPlan,
        PublishApprovalPolicy,
    )
    from tests.integration.test_project_engineering_plan_acceptance import admission

    _, _, _, _, command, issue = engineering_revisit(native_engine, scope)
    with Session(native_engine) as db:
        head = db.get(ProjectEngineeringPlan, issue.plan_id)
        policy = db.get(EngineeringPolicyHead, scope.project_id)
        revision = db.get(EngineeringPlanRevision, issue.revision_id)
        assert head and policy and revision
        content = stored_engineering_content(db, revision)
        generation = head.generation
        policy_generation = policy.generation
    if retained_change == "later_draft":
        future = content.model_copy(
            update={
                "scope_statement": "Future draft does not revoke the approved historical scope"
            }
        )
        draft = DraftEngineeringPlan(
            context=engineering_context(scope.author_id),
            access=admission(scope.author_id),
            plan_id=issue.plan_id,
            project_id=scope.project_id,
            task_id=scope.task_id,
            expected_project_updated_at=scope.project_token,
            expected_task_updated_at=scope.task_token,
            expected_generation=generation,
            content=future,
        )
        with Session(native_engine) as db:
            plans.draft_revision(db, command=draft)
    else:
        publish = PublishApprovalPolicy(
            context=engineering_context(scope.reviewer_id),
            access=admission(scope.reviewer_id),
            project_id=scope.project_id,
            expected_project_updated_at=scope.project_token,
            expected_generation=policy_generation,
            independent_reviewer_required=False,
        )
        with Session(native_engine) as db:
            plans.publish_policy(db, command=publish)
    with Session(native_engine) as db:
        head = db.get(ProjectEngineeringPlan, issue.plan_id)
        assert head and head.current_approval_id == issue.approval_id
        if retained_change == "later_draft":
            assert head.latest_revision_id != issue.revision_id
    with Session(native_engine) as db:
        approved = owner.review_failed_scope_replacement(db, command=command)
    assert approved.replacement_enrollment_id == command.replacement_enrollment_id


def test_review_and_plan_revocation_serialize_without_deadlock(
    native_engine: Engine, scope: NativeScope
):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Barrier

    from app.models.project_engineering_plan import (
        EngineeringPolicyHead,
        ProjectEngineeringPlan,
    )
    from app.services import project_engineering_plans as plans
    from app.services.field.completion_review_contracts import (
        CurrentReplacementEvidenceQuery,
    )
    from app.services.project_engineering_plan_contracts import (
        DecideEngineeringPlan,
        PlanDecisionKind,
    )
    from tests.integration.test_project_engineering_plan_acceptance import admission

    _, _, _, _, command, issue = engineering_revisit(native_engine, scope)
    with Session(native_engine) as db:
        head = db.get(ProjectEngineeringPlan, issue.plan_id)
        policy = db.get(EngineeringPolicyHead, scope.project_id)
        source = db.get(CompletionEnrollment, command.source_enrollment_id)
        assert head and policy and source
        source_work_order_id = source.work_order_id
        revoke = DecideEngineeringPlan(
            context=engineering_context(scope.reviewer_id),
            access=admission(scope.reviewer_id),
            project_id=scope.project_id,
            task_id=scope.task_id,
            expected_project_updated_at=scope.project_token,
            expected_task_updated_at=scope.task_token,
            plan_id=issue.plan_id,
            revision_id=issue.revision_id,
            expected_digest=issue.digest,
            expected_policy_revision_id=policy.current_revision_id,
            expected_generation=head.generation,
            decision=PlanDecisionKind.revoke,
        )
    barrier = Barrier(2)

    def approve():
        with Session(native_engine) as db:
            barrier.wait(timeout=10)
            try:
                return owner.review_failed_scope_replacement(db, command=command)
            except DomainError:
                return None

    def revoke_plan():
        with Session(native_engine) as db:
            barrier.wait(timeout=10)
            return plans.decide(db, command=revoke)

    with ThreadPoolExecutor(max_workers=2) as pool:
        approved_future = pool.submit(approve)
        revoked_future = pool.submit(revoke_plan)
        approved = approved_future.result(timeout=30)
        revoked = revoked_future.result(timeout=30)
    assert revoked.decision_id is not None
    with Session(native_engine) as db:
        head = db.get(ProjectEngineeringPlan, issue.plan_id)
        assert head and head.current_approval_id is None
        if approved is not None:
            with pytest.raises(DomainError):
                owner.current_replacement_evidence(
                    db,
                    query=CurrentReplacementEvidenceQuery(
                        access=command.access,
                        source_work_order_id=source_work_order_id,
                        decision_id=approved.decision_id,
                        failure_basis=command.failure_basis,
                    ),
                )


def test_graph_workspace_and_concurrent_engineering_enrollment_serialize(
    native_engine: Engine, scope: NativeScope
):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Barrier

    prepared, _, _, _, command, issue = engineering_revisit(native_engine, scope)
    with Session(native_engine) as db:
        third = handoff.issue(
            db, command=replace(issue, context=context(scope.reviewer_id, PERMISSION))
        )
    with Session(native_engine) as db:
        row = db.get(WorkOrder, third.work_order_id)
        profile = db.scalar(
            select(TechnicianProfile).where(
                TechnicianProfile.system_user_id
                == prepared.submission.requester_system_user_id
            )
        )
        source = db.get(CompletionEnrollment, command.source_enrollment_id)
        assert row and profile and source
        token = row.updated_at
        profile_id = profile.id
        source_public = source.public_id
    with Session(native_engine) as db:
        work_order_commands.assign(
            db,
            command=WorkOrderAssignmentCommand(
                access=command.access,
                work_order_public_id=third.public_id,
                target=TechnicianAssignmentTarget(profile_id),
                expected_revision=token,
                reason="Assign concurrent graph enrollment candidate",
            ),
            context=context(scope.reviewer_id, "operations:dispatch:assign"),
        )
    enrollment = EnrollCompletionReview(
        context=context(scope.reviewer_id, WRITE),
        access=command.access,
        work_order_public_id=third.public_id,
        provenance=CompletionProvenance.engineering_issuance,
        engineering_issuance_id=third.issuance_id,
        policy_scope=CompletionPolicyScope(
            kind=CompletionPolicyScopeKind.project, project_id=scope.project_id
        ),
        criteria=(),
    )
    query = FailedScopeReplacementWorkspaceQuery(
        access=command.access,
        work_order_public_id=source_public,
        purpose=ReplacementWorkspacePurpose.read,
    )
    barrier = Barrier(2)

    def enroll_third():
        with Session(native_engine) as db:
            barrier.wait(timeout=10)
            return owner.enroll(db, command=enrollment)

    def graph():
        with Session(native_engine) as db:
            barrier.wait(timeout=10)
            return owner.failed_scope_replacement_workspace(db, query=query)

    with ThreadPoolExecutor(max_workers=2) as pool:
        enrolled_future = pool.submit(enroll_third)
        workspace_future = pool.submit(graph)
        enrolled = enrolled_future.result(timeout=30)
        workspace = workspace_future.result(timeout=30)
    assert workspace.source.enrollment_id == command.source_enrollment_id
    with Session(native_engine) as db:
        current = owner.failed_scope_replacement_workspace(db, query=query)
    assert enrolled.enrollment_id in {
        candidate.enrollment_id for candidate in current.candidates
    }
