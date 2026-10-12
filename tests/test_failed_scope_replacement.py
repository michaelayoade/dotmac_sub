"""Fast, non-authoritative SQLite lifecycle checks; migrated PG is separate."""

from dataclasses import dataclass, field, replace
from datetime import UTC, datetime
from uuid import UUID, uuid4

import pytest
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.models.dispatch import TechnicianProfile
from app.models.field_completion_review import (
    CompletionEnrollment,
    CompletionReplacementDecision,
    CompletionReplacementReceipt,
)
from app.models.field_job_event import FieldJobEvent
from app.models.subscriber import Subscriber
from app.models.support import SupportResolutionReplacementLineage, Ticket
from app.models.work_order import WorkOrder
from app.services import project_engineering_plans, support
from app.services.audit_adapter import AuditRecord
from app.services.domain_errors import DomainError
from app.services.events import EventType
from app.services.field import completion_review as owner
from app.services.field.completion_review_contracts import (
    READ,
    WRITE,
    CompletionPolicyScope,
    CompletionPolicyScopeKind,
    CompletionProvenance,
    CriterionCoverage,
    CriterionObservation,
    CriterionResult,
    CriterionVerdict,
    DecideCompletionReview,
    EnrollCompletionReview,
    FailedScopeReplacementWorkspaceQuery,
    IssuedCompletionCriterion,
    PublishCompletionReviewPolicy,
    ReplacementState,
    ReplacementWorkspacePurpose,
    ReviewFailedScopeReplacement,
    RevokeFailedScopeReplacement,
    SubmitCompletionEvidence,
    SupportTicketCompletionPreviewQuery,
    UnableToCompleteFailure,
)
from app.services.field.work_order_access import FieldActor, FieldActorKind
from app.services.owner_commands import CommandContext
from app.services.permission_admission import PermissionAdmission
from app.services.staff_permission_admission import AuthorizedStaffPerson
from app.services.work_order_assignment_contracts import (
    CurrentWorkOrderAssignment,
    CurrentWorkOrderAssignmentQuery,
)
from tests.staff_identity_fixtures import add_bound_staff_user


def context(actor: UUID) -> CommandContext:
    return CommandContext.system(
        actor=str(actor),
        scope=WRITE,
        reason="Reviewed failed scope coverage",
        idempotency_key=str(uuid4()),
    )


@dataclass(kw_only=True)
class ReplacementRig:
    db: Session
    staff: UUID
    person: UUID
    ticket: UUID
    technician: UUID
    profile: UUID
    actor: FieldActor
    assignments: dict[UUID, CurrentWorkOrderAssignment]
    command: ReviewFailedScopeReplacement
    denied: bool = False
    events: list[EventType] = field(default_factory=list)
    audits: list[AuditRecord] = field(default_factory=list)


@pytest.fixture
def replacement_rig(
    db_session: Session, subscriber: Subscriber, monkeypatch: pytest.MonkeyPatch
) -> ReplacementRig:
    db = db_session
    staff, person = add_bound_staff_user(db)
    technician, field_person = add_bound_staff_user(db)
    profile = TechnicianProfile(system_user_id=technician.id, person_id=technician.id)
    ticket = Ticket(title="Failed visit", subscriber_id=subscriber.id, status="open")
    db.add_all((profile, ticket))
    db.flush()
    assignments: dict[UUID, CurrentWorkOrderAssignment] = {}
    actor = FieldActor(
        kind=FieldActorKind.technician,
        system_user_id=technician.id,
        technician_id=profile.id,
        person_id=field_person.id,
    )
    admitted = PermissionAdmission(
        staff.id, "system_user", frozenset((READ, WRITE, "support:ticket:update"))
    )
    state: ReplacementRig | None = None

    def staff_gate(
        db: Session, *, admission: PermissionAdmission, permission: str, write: bool
    ) -> AuthorizedStaffPerson:
        if (
            state is not None and state.denied
        ) or permission not in admission.admitted_permissions:
            raise owner._error("denied")
        user = db.get(type(staff), admission.principal_id)
        assert user is not None and user.person_party_id is not None
        return AuthorizedStaffPerson(user.id, user.person_party_id)

    monkeypatch.setattr(owner, "revalidate_staff_admission", staff_gate)
    monkeypatch.setattr(support, "revalidate_staff_admission", staff_gate)
    monkeypatch.setattr(
        project_engineering_plans, "revalidate_staff_admission", staff_gate
    )

    def assignment(
        db: Session, *, query: CurrentWorkOrderAssignmentQuery
    ) -> CurrentWorkOrderAssignment | None:
        return assignments.get(query.work_order_id)

    monkeypatch.setattr(owner.work_order_commands, "current_assignment", assignment)
    monkeypatch.setattr(owner, "resolve_field_actor", lambda db, query: actor)
    monkeypatch.setattr(owner, "_field_admit", lambda db, **kwargs: actor)
    monkeypatch.setattr(
        owner,
        "emit_event",
        lambda db, event_type, details, **kwargs: (
            state.events.append(event_type) if state else None
        ),
    )
    monkeypatch.setattr(
        owner.audit_adapter,
        "stage",
        lambda db, record: state.audits.append(record) if state else None,
    )
    orders = []
    for label, status in (
        ("Failed original", "canceled"),
        ("Replacement visit", "scheduled"),
    ):
        row = WorkOrder(
            title=label,
            subscriber_id=subscriber.id,
            origin_ticket_id=ticket.id,
            status=status,
            work_order_kind="customer",
        )
        db.add(row)
        db.flush()
        assignments[row.id] = CurrentWorkOrderAssignment(
            queue_id=uuid4(),
            technician_id=profile.id,
            vendor_id=None,
            generation=uuid4(),
            effective_at=datetime.now(UTC),
        )
        orders.append(row)
    source, replacement = orders
    generation = assignments[source.id].generation
    assert generation is not None
    event = FieldJobEvent(
        work_order_mirror_id=source.id,
        author_technician_id=profile.id,
        person_id=technician.id,
        system_user_id=technician.id,
        event="unable_to_complete",
        new_status="canceled",
        assignment_generation=generation,
        occurred_at=datetime.now(UTC),
        received_at=datetime.now(UTC),
        client_event_id=uuid4(),
    )
    db.add(event)
    db.flush()
    event_id, staff_id, person_id, ticket_id = event.id, staff.id, person.id, ticket.id
    technician_id, profile_id = technician.id, profile.id
    db.commit()
    enrollments = []
    for row in orders:
        public_id = row.public_id
        scope = CompletionPolicyScope(
            kind=CompletionPolicyScopeKind.work_order,
            work_order_public_id=public_id,
        )
        db.commit()
        policy = owner.publish_policy(
            db,
            command=PublishCompletionReviewPolicy(
                context=context(staff_id),
                access=admitted,
                scope=scope,
                expected_generation=0,
                independent_reviewer_required=True,
            ),
        )
        criteria = (
            IssuedCompletionCriterion(
                id=uuid4(),
                order=0,
                statement="Repair customer service",
                verification_method="Measure service",
            ),
        )
        enrolled = owner.enroll(
            db,
            command=EnrollCompletionReview(
                context=context(staff_id),
                access=admitted,
                work_order_public_id=public_id,
                provenance=CompletionProvenance.native_work_order,
                policy_scope=scope,
                criteria=criteria,
            ),
        )
        assert (
            enrolled.enrollment_id
            and enrolled.criteria_revision_id
            and policy.policy_revision_id
        )
        enrollments.append((enrolled, policy, criteria))
    source_enrollment, source_policy, source_criteria = enrollments[0]
    replacement_enrollment, replacement_policy, replacement_criteria = enrollments[1]
    replacement_generation = assignments[replacement.id].generation
    assert replacement_generation is not None
    command = ReviewFailedScopeReplacement(
        context=context(staff_id),
        access=admitted,
        source_enrollment_id=source_enrollment.enrollment_id,
        replacement_enrollment_id=replacement_enrollment.enrollment_id,
        expected_generation=0,
        failure_basis=UnableToCompleteFailure(event_id, generation),
        source_criteria_revision_id=source_enrollment.criteria_revision_id,
        replacement_criteria_revision_id=replacement_enrollment.criteria_revision_id,
        source_policy_revision_id=source_policy.policy_revision_id,
        replacement_policy_revision_id=replacement_policy.policy_revision_id,
        source_assignment_generation=generation,
        replacement_assignment_generation=replacement_generation,
        coverage=(
            CriterionCoverage(source_criteria[0].id, replacement_criteria[0].id),
        ),
    )
    state = ReplacementRig(
        db=db,
        staff=staff_id,
        person=person_id,
        ticket=ticket_id,
        technician=technician_id,
        profile=profile_id,
        actor=actor,
        assignments=assignments,
        command=command,
    )
    db.commit()
    return state


def accept_replacement(rig: ReplacementRig) -> None:
    db = rig.db
    enrollment = db.get(CompletionEnrollment, rig.command.replacement_enrollment_id)
    assert enrollment is not None
    row = db.get(WorkOrder, enrollment.work_order_id)
    assert row is not None
    row.status = "completed"
    event = FieldJobEvent(
        work_order_mirror_id=row.id,
        author_technician_id=rig.profile,
        person_id=rig.technician,
        system_user_id=rig.technician,
        event="complete",
        new_status="completed",
        assignment_generation=rig.command.replacement_assignment_generation,
        occurred_at=datetime.now(UTC),
        received_at=datetime.now(UTC),
        client_event_id=uuid4(),
    )
    db.add(event)
    db.flush()
    event_id = event.id
    db.commit()
    submitted = owner.submit_evidence(
        db,
        command=SubmitCompletionEvidence(
            context=replace(context(rig.technician), scope="field:completion:submit"),
            requester_system_user_id=rig.technician,
            enrollment_id=rig.command.replacement_enrollment_id,
            criteria_revision_id=rig.command.replacement_criteria_revision_id,
            policy_revision_id=rig.command.replacement_policy_revision_id,
            completed_event_id=event_id,
            assignment_generation=rig.command.replacement_assignment_generation,
            expected_generation=0,
            observations=tuple(
                CriterionObservation(
                    item.replacement_criterion_id, "Measured service restored"
                )
                for item in rig.command.coverage
            ),
        ),
    )
    assert submitted.submission_id is not None
    owner.decide(
        db,
        command=DecideCompletionReview(
            context=context(rig.staff),
            access=rig.command.access,
            enrollment_id=rig.command.replacement_enrollment_id,
            submission_id=submitted.submission_id,
            expected_generation=submitted.generation,
            policy_revision_id=rig.command.replacement_policy_revision_id,
            results=tuple(
                CriterionResult(
                    item.replacement_criterion_id, CriterionVerdict.passed, "Verified"
                )
                for item in rig.command.coverage
            ),
        ),
    )


def test_review_does_not_waive_acceptance_and_pins_original_scope(
    replacement_rig: ReplacementRig,
) -> None:
    rig = replacement_rig
    outcome = owner.review_failed_scope_replacement(rig.db, command=rig.command)
    assert outcome.state == ReplacementState.approved
    with pytest.raises(DomainError, match="completion pending"):
        owner.preview_ticket_completion_basis(
            rig.db,
            query=SupportTicketCompletionPreviewQuery(
                ticket_id=rig.ticket, access=rig.command.access
            ),
        )
    rig.db.rollback()
    accept_replacement(rig)
    basis = owner.preview_ticket_completion_basis(
        rig.db,
        query=SupportTicketCompletionPreviewQuery(
            ticket_id=rig.ticket, access=rig.command.access
        ),
    )
    assert len(basis.entries) == 2
    source = next(
        item
        for item in basis.entries
        if item.enrollment_id == rig.command.source_enrollment_id
    )
    assert source.effective_enrollment_id == rig.command.replacement_enrollment_id
    assert source.replacement_decision_ids == (outcome.decision_id,)
    source_order = rig.db.get(WorkOrder, source.work_order_id)
    assert source_order is not None and source_order.status == "canceled"


@pytest.mark.parametrize("drift", ["reassignment", "revoke", "policy", "criteria"])
def test_current_basis_refuses_replacement_drift(
    replacement_rig: ReplacementRig, drift: str
) -> None:
    rig = replacement_rig
    outcome = owner.review_failed_scope_replacement(rig.db, command=rig.command)
    accept_replacement(rig)
    if drift == "revoke":
        owner.revoke_failed_scope_replacement(
            rig.db,
            command=RevokeFailedScopeReplacement(
                context=context(rig.staff),
                access=rig.command.access,
                source_enrollment_id=rig.command.source_enrollment_id,
                decision_id=outcome.decision_id,
                expected_generation=outcome.generation,
            ),
        )
    elif drift == "reassignment":
        source = rig.db.get(CompletionEnrollment, rig.command.source_enrollment_id)
        assert source is not None
        rig.assignments[source.work_order_id] = replace(
            rig.assignments[source.work_order_id], generation=uuid4()
        )
        rig.db.commit()
    elif drift == "policy":
        source = rig.db.get(CompletionEnrollment, rig.command.source_enrollment_id)
        assert source is not None
        public_id = source.public_id
        rig.db.commit()
        owner.publish_policy(
            rig.db,
            command=PublishCompletionReviewPolicy(
                context=context(rig.staff),
                access=rig.command.access,
                scope=CompletionPolicyScope(
                    kind=CompletionPolicyScopeKind.work_order,
                    work_order_public_id=public_id,
                ),
                expected_generation=1,
                independent_reviewer_required=False,
            ),
        )
    else:
        from app.services.field.completion_review_contracts import (
            PublishCompletionCriteria,
        )

        owner.publish_criteria(
            rig.db,
            command=PublishCompletionCriteria(
                context=context(rig.staff),
                access=rig.command.access,
                enrollment_id=rig.command.source_enrollment_id,
                expected_generation=1,
                criteria=(
                    IssuedCompletionCriterion(
                        id=uuid4(),
                        order=0,
                        statement="Changed scope",
                        verification_method="Measure",
                    ),
                ),
            ),
        )
    with pytest.raises(DomainError) as refusal:
        owner.preview_ticket_completion_basis(
            rig.db,
            query=SupportTicketCompletionPreviewQuery(
                ticket_id=rig.ticket, access=rig.command.access
            ),
        )
    assert (
        refusal.value.code
        == "operations.field_completion."
        + {
            "reassignment": "assignment_generation_mismatch",
            "revoke": "completion_pending",
            "policy": "stale_policy",
            "criteria": "stale_criteria",
        }[drift]
    )


@pytest.mark.parametrize(
    "mode",
    [
        "missing_coverage",
        "wrong_coverage",
        "wrong_failure",
        "self",
        "stale_generation",
        "wrong_route",
    ],
)
def test_review_refuses_inexact_inputs_atomically(
    replacement_rig: ReplacementRig, mode: str
) -> None:
    rig = replacement_rig
    command = rig.command
    if mode == "missing_coverage":
        command = replace(command, coverage=())
    if mode == "wrong_coverage":
        command = replace(
            command,
            coverage=(
                CriterionCoverage(command.coverage[0].source_criterion_id, uuid4()),
            ),
        )
    if mode == "wrong_failure":
        command = replace(
            command,
            failure_basis=UnableToCompleteFailure(
                uuid4(), command.source_assignment_generation
            ),
        )
    if mode == "self":
        command = replace(
            command, replacement_enrollment_id=command.source_enrollment_id
        )
    if mode == "stale_generation":
        command = replace(command, expected_generation=1)
    if mode == "wrong_route":
        command = replace(command, expected_work_order_public_id="unrelated")
    with pytest.raises(DomainError):
        owner.review_failed_scope_replacement(rig.db, command=command)
    assert (
        rig.db.scalar(select(func.count()).select_from(CompletionReplacementDecision))
        == 0
    )
    assert (
        rig.db.scalar(select(func.count()).select_from(CompletionReplacementReceipt))
        == 0
    )
    assert not rig.events and not rig.audits


def test_replay_has_no_repeated_effect_and_rechecks_permission(
    replacement_rig: ReplacementRig,
) -> None:
    rig = replacement_rig
    first = owner.review_failed_scope_replacement(rig.db, command=rig.command)
    replay = owner.review_failed_scope_replacement(rig.db, command=rig.command)
    assert replay.replayed and replay.decision_id == first.decision_id
    assert len(rig.events) == len(rig.audits) == 1
    rig.denied = True
    with pytest.raises(DomainError):
        owner.review_failed_scope_replacement(rig.db, command=rig.command)


def test_replacement_independence_follows_current_policy(
    replacement_rig: ReplacementRig,
) -> None:
    rig = replacement_rig
    field_access = replace(rig.command.access, principal_id=rig.technician)
    command = replace(rig.command, access=field_access, context=context(rig.technician))
    with pytest.raises(DomainError, match="independent reviewer required"):
        owner.review_failed_scope_replacement(rig.db, command=command)
    for enrollment_id in (
        rig.command.source_enrollment_id,
        rig.command.replacement_enrollment_id,
    ):
        enrolled = rig.db.get(CompletionEnrollment, enrollment_id)
        assert enrolled is not None
        public_id = enrolled.public_id
        rig.db.commit()
        owner.publish_policy(
            rig.db,
            command=PublishCompletionReviewPolicy(
                context=context(rig.staff),
                access=rig.command.access,
                scope=CompletionPolicyScope(
                    kind=CompletionPolicyScopeKind.work_order,
                    work_order_public_id=public_id,
                ),
                expected_generation=1,
                independent_reviewer_required=False,
            ),
        )
    workspace = owner.failed_scope_replacement_workspace(
        rig.db,
        query=FailedScopeReplacementWorkspaceQuery(
            access=field_access,
            work_order_public_id=rig.db.get(
                CompletionEnrollment, rig.command.source_enrollment_id
            ).public_id,
            purpose=ReplacementWorkspacePurpose.review,
        ),
    )
    assert workspace.review_readiness.is_ready and workspace.candidates
    rig.db.commit()
    command = replace(
        command,
        source_policy_revision_id=workspace.source.policy_revision_id,
        replacement_policy_revision_id=workspace.candidates[0].policy_revision_id,
    )
    assert (
        owner.review_failed_scope_replacement(rig.db, command=command).state
        == ReplacementState.approved
    )


def test_confirmation_retains_and_revalidates_ordered_lineage(
    replacement_rig: ReplacementRig,
) -> None:
    rig = replacement_rig
    reviewed = owner.review_failed_scope_replacement(rig.db, command=rig.command)
    accept_replacement(rig)
    _, token = support.Tickets.request_resolution_confirmation(
        rig.db, str(rig.ticket), grace_hours=24
    )
    assert (
        rig.db.scalar(
            select(func.count()).select_from(SupportResolutionReplacementLineage)
        )
        == 1
    )
    rig.db.commit()
    owner.revoke_failed_scope_replacement(
        rig.db,
        command=RevokeFailedScopeReplacement(
            context=context(rig.staff),
            access=rig.command.access,
            source_enrollment_id=rig.command.source_enrollment_id,
            decision_id=reviewed.decision_id,
            expected_generation=reviewed.generation,
        ),
    )
    with pytest.raises(DomainError):
        support.Tickets.confirm_resolution(rig.db, token)


def test_reviewer_cannot_cover_their_own_unexecuted_replacement_assignment(
    replacement_rig: ReplacementRig,
) -> None:
    rig = replacement_rig
    profile = TechnicianProfile(person_id=rig.staff, system_user_id=rig.staff)
    rig.db.add(profile)
    rig.db.flush()
    enrolled = rig.db.get(CompletionEnrollment, rig.command.replacement_enrollment_id)
    assert enrolled is not None
    rig.assignments[enrolled.work_order_id] = replace(
        rig.assignments[enrolled.work_order_id], technician_id=profile.id
    )
    rig.db.commit()
    with pytest.raises(DomainError, match="independent reviewer required"):
        owner.review_failed_scope_replacement(rig.db, command=rig.command)


def test_historical_workspace_and_revocation_survive_retargeted_scope(
    replacement_rig: ReplacementRig,
) -> None:
    rig = replacement_rig
    reviewed = owner.review_failed_scope_replacement(rig.db, command=rig.command)
    source = rig.db.get(CompletionEnrollment, rig.command.source_enrollment_id)
    assert source is not None
    order = rig.db.get(WorkOrder, source.work_order_id)
    assert order is not None
    public_id = source.public_id
    order.subscriber_id = None
    rig.db.commit()
    workspace = owner.failed_scope_replacement_workspace(
        rig.db,
        query=FailedScopeReplacementWorkspaceQuery(
            access=rig.command.access,
            work_order_public_id=public_id,
            purpose=ReplacementWorkspacePurpose.revoke,
        ),
    )
    assert workspace.history and not workspace.review_readiness.is_ready
    assert workspace.revoke_readiness.is_ready
    rig.db.commit()
    revoked = owner.revoke_failed_scope_replacement(
        rig.db,
        command=RevokeFailedScopeReplacement(
            context=context(rig.staff),
            access=rig.command.access,
            source_enrollment_id=rig.command.source_enrollment_id,
            decision_id=reviewed.decision_id,
            expected_generation=reviewed.generation,
        ),
    )
    assert revoked.state == ReplacementState.revoked
