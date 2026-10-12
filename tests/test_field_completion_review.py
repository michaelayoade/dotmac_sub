"""Fast SQLite behavior only; PostgreSQL migration/concurrency is separate acceptance."""

import hashlib
from collections.abc import Iterator
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime
from decimal import Decimal
from uuid import UUID, uuid4

import pytest
from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import Session

from app.db import Base
from app.models.field_attachment import FieldAttachment
from app.models.field_completion_review import (
    CompletionCriteriaHead,
    CompletionCriteriaRevision,
    CompletionCriterion,
    CompletionCriterionResult,
    CompletionEnrollment,
    CompletionEvidenceFile,
    CompletionObservation,
    CompletionReplacementCoverage,
    CompletionReplacementDecision,
    CompletionReplacementHead,
    CompletionReplacementReceipt,
    CompletionReviewDecision,
    CompletionReviewHead,
    CompletionReviewPolicy,
    CompletionReviewPolicyHead,
    CompletionReviewReceipt,
    CompletionSubmission,
)
from app.models.field_job_event import FieldJobEvent
from app.models.project import Project, ProjectInfrastructure, ProjectTask
from app.models.project_engineering_plan import EngineeringPlanCriterion
from app.models.project_field_completion import ProjectFieldCompletionRequirement
from app.models.project_work_order_issuance import ProjectWorkOrderIssuance
from app.models.stored_file import StoredFile
from app.models.stored_file_retention import StoredFileRetention
from app.models.support import Ticket
from app.models.system_user import SystemUser
from app.models.work_order import WorkOrder
from app.schemas.infrastructure import InfrastructureReference, InfrastructureType
from app.services import project_engineering_plans, staff_permission_admission
from app.services.audit_adapter import AuditRecord
from app.services.domain_errors import DomainError
from app.services.events import Event, EventType
from app.services.field import completion_review as owner
from app.services.field.completion_review_contracts import (
    READ,
    WRITE,
    CompletionEvidenceQuery,
    CompletionPolicyPurpose,
    CompletionPolicyQuery,
    CompletionPolicyScope,
    CompletionPolicyScopeKind,
    CompletionProvenance,
    CompletionReviewQuery,
    CompletionWorkspacePurpose,
    CompletionWorkspaceQuery,
    CriterionObservation,
    CriterionResult,
    CriterionVerdict,
    DecideCompletionReview,
    EnrollCompletionReview,
    FieldSubmissionPreviewQuery,
    IssuedCompletionCriterion,
    PublishCompletionCriteria,
    PublishCompletionReviewPolicy,
    ReviewState,
    RevokeCompletionReview,
    SubmitCompletionEvidence,
)
from app.services.field.work_order_access import (
    FieldActor,
    FieldActorKind,
    FieldWorkOrderScope,
    ResolveFieldActor,
)
from app.services.file_storage import file_uploads
from app.services.object_storage import StreamResult
from app.services.owner_commands import CommandContext
from app.services.permission_admission import PermissionAdmission
from app.services.project_engineering_plan_contracts import (
    CompletionEngineeringPlanQuery,
    CurrentEngineeringPlanApproval,
    EngineeringCriterion,
    EngineeringPlanContent,
    EngineeringTechnology,
    GeoPoint,
    SurveyedPlanLocation,
)
from app.services.projects import (
    ProjectEngineeringOrigin,
    ProjectEngineeringRevocationScope,
)
from app.services.staff_permission_admission import AuthorizedStaffPerson
from app.services.work_order_assignment_contracts import (
    CurrentWorkOrderAssignment,
    CurrentWorkOrderAssignmentQuery,
)


@dataclass(kw_only=True)
class Rig:
    db: Session
    staff: UUID
    person: UUID
    field: UUID
    project: UUID
    issuance: UUID
    criteria: tuple[UUID, ...]
    generation: UUID
    policy: UUID
    enrollment: UUID | None = None
    criteria_revision: UUID | None = None
    completed_event: UUID | None = None
    denied: bool = False
    engineering_stale: bool = False
    actor_revoked: bool = False
    events: list[EventType] = field(default_factory=list)
    audits: list[AuditRecord] = field(default_factory=list)


def context(actor: UUID, scope: str) -> CommandContext:
    return CommandContext(
        command_id=uuid4(),
        correlation_id=uuid4(),
        actor=str(actor),
        scope=scope,
        reason="Explicit reviewed evidence",
        idempotency_key=str(uuid4()),
    )


def access(user: UUID) -> PermissionAdmission:
    return PermissionAdmission(user, "system_user", frozenset({READ, WRITE}))


def historical_scope(
    project_id: UUID, task_id: UUID | None
) -> ProjectEngineeringRevocationScope:
    return ProjectEngineeringRevocationScope(
        project_id=project_id,
        task_id=task_id,
        project_updated_at=datetime(2026, 10, 11, tzinfo=UTC),
        task_updated_at=None,
    )


@pytest.fixture
def rig(
    monkeypatch: pytest.MonkeyPatch, request: pytest.FixtureRequest
) -> Iterator[Rig]:
    native = getattr(request, "param", "engineering") == "native"

    class EvidenceStorage:
        def stream(self, key: str) -> StreamResult:
            return StreamResult(
                chunks=iter((b"proof",)),
                content_type="application/pdf",
                content_length=5,
            )

    monkeypatch.setattr(file_uploads, "storage", EvidenceStorage())
    engine = create_engine("sqlite://")
    tables = [
        value.__table__
        for value in (
            WorkOrder,
            Project,
            ProjectTask,
            ProjectInfrastructure,
            ProjectFieldCompletionRequirement,
            Ticket,
            CompletionEnrollment,
            CompletionCriteriaRevision,
            CompletionCriteriaHead,
            CompletionCriterion,
            FieldJobEvent,
            ProjectWorkOrderIssuance,
            EngineeringPlanCriterion,
            StoredFile,
            StoredFileRetention,
            FieldAttachment,
            SystemUser,
            CompletionReviewPolicy,
            CompletionReviewPolicyHead,
            CompletionSubmission,
            CompletionObservation,
            CompletionEvidenceFile,
            CompletionReviewDecision,
            CompletionCriterionResult,
            CompletionReviewHead,
            CompletionReviewReceipt,
            CompletionReplacementDecision,
            CompletionReplacementCoverage,
            CompletionReplacementHead,
            CompletionReplacementReceipt,
        )
    ]
    Base.metadata.create_all(engine, tables=tables)
    db = Session(engine)
    staff, person, technician, project, issuance, revision, generation = (
        uuid4() for _ in range(7)
    )
    db.add(
        Project(
            id=project,
            name="Actual enrolled unit project",
            status="active",
            is_active=True,
        )
    )
    work_order = WorkOrder(
        id=uuid4(),
        public_id="sub-completion-test",
        title="Issued native work",
        project_id=project,
        work_order_kind="infrastructure",
        purpose="infrastructure_rollout",
        access_technology="fiber",
        template_version=1,
        classification_source="explicit_operator_selection",
    )
    db.add(work_order)
    db.add(
        SystemUser(
            id=technician,
            first_name="Completion",
            last_name="Field",
            email="completion-field@example.test",
            person_party_id=uuid4(),
            party_bound_at=datetime(2026, 10, 11, tzinfo=UTC),
            party_binding_source="test-fixture",
            party_binding_reason="Reviewed completion actor binding",
            is_active=True,
        )
    )
    if not native:
        db.add(
            ProjectWorkOrderIssuance(
                id=issuance,
                idempotency_key=str(uuid4()),
                fingerprint="a" * 64,
                work_order_id=work_order.id,
                public_id=work_order.public_id,
                project_id=project,
                plan_id=uuid4(),
                revision_id=revision,
                approval_id=uuid4(),
                digest="b" * 64,
                infrastructure_type="cabinet",
                infrastructure_id=uuid4(),
                purpose="infrastructure_rollout",
                technology="fiber",
                actor_user_id=staff,
                actor_person_id=person,
                reason="Issued approved scope",
                command_id=uuid4(),
            )
        )
    criteria = (uuid4(), uuid4())
    for order, criterion in enumerate(criteria):
        db.add(
            EngineeringPlanCriterion(
                revision_id=revision,
                id=criterion,
                order=order,
                statement="Verify approved criterion",
                verification_method="Inspect",
            )
        )
    db.commit()
    result = Rig(
        db=db,
        staff=staff,
        person=person,
        field=technician,
        project=project,
        issuance=issuance,
        criteria=criteria,
        generation=generation,
        policy=uuid4(),
    )

    def staff_gate(
        _db: Session, *, admission: PermissionAdmission, permission: str, write: bool
    ) -> AuthorizedStaffPerson:
        if result.denied or permission not in admission.admitted_permissions:
            raise DomainError(
                code="auth.staff_permission_admission.denied", message="Denied"
            )
        assert permission in {READ, WRITE}
        return AuthorizedStaffPerson(admission.principal_id, result.person)

    actor = FieldActor(
        kind=FieldActorKind.vendor,
        system_user_id=technician,
        vendor_user_id=uuid4(),
        native_vendor_id=uuid4(),
    )

    def field_gate(
        _db: Session,
        *,
        command: SubmitCompletionEvidence,
        enrollment: CompletionEnrollment,
        lock: bool,
    ) -> FieldActor:
        if result.actor_revoked or command.assignment_generation != result.generation:
            raise owner._error("assignment_generation_mismatch")
        return actor

    def resolve_actor(_db: Session, query: ResolveFieldActor) -> FieldActor:
        return actor

    def engineering_gate(
        _db: Session, *, query: CompletionEngineeringPlanQuery
    ) -> CurrentEngineeringPlanApproval:
        if result.engineering_stale:
            raise DomainError(
                code="operations.project_engineering_plans.unapproved",
                message="Unapproved",
            )
        return CurrentEngineeringPlanApproval(
            plan_id=query.plan_id,
            project_id=query.project_id,
            task_id=query.task_id,
            revision_id=query.expected_revision_id,
            approval_id=query.expected_approval_id,
            policy_revision_id=uuid4(),
            digest=query.expected_digest,
            content=EngineeringPlanContent(
                technology=EngineeringTechnology.fiber,
                infrastructure=InfrastructureReference(
                    type=InfrastructureType.cabinet, id=uuid4()
                ),
                location=SurveyedPlanLocation(
                    description="Synthetic reviewed site",
                    point=GeoPoint(latitude=Decimal("9"), longitude=Decimal("7")),
                ),
                scope_statement="Synthetic immutable scope",
                criteria=tuple(
                    EngineeringCriterion(
                        id=value,
                        order=order,
                        statement="Approved criterion",
                        verification_method="Inspect",
                    )
                    for order, value in enumerate(result.criteria)
                ),
            ),
            author_person_id=uuid4(),
            reviewer_person_id=result.person,
            generation=1,
        )

    def origin_gate(
        _db: Session, *, project_id: UUID, task_id: UUID | None, lock: bool
    ) -> ProjectEngineeringOrigin:
        return ProjectEngineeringOrigin(
            project_id=project_id,
            task_id=task_id,
            project_updated_at=datetime(2026, 10, 11, tzinfo=UTC),
            task_updated_at=None,
            project_template_id=None,
            applied_template_revision=None,
            task_template_id=None,
            task_template_revision=None,
            infrastructure=InfrastructureReference(
                type=InfrastructureType.cabinet, id=uuid4()
            ),
            issuance_eligible=True,
        )

    def historical_gate(
        _db: Session, *, project_id: UUID, task_id: UUID | None
    ) -> ProjectEngineeringRevocationScope:
        return historical_scope(project_id, task_id)

    def event_gate(
        _db: Session,
        event_type: EventType,
        payload: dict[str, object],
        *,
        actor: str | None,
    ) -> Event:
        result.events.append(event_type)
        return Event(event_type=event_type, payload=payload)

    def audit_gate(_db: Session, record: AuditRecord) -> None:
        result.audits.append(record)

    def visit_gate(_db: Session, query: FieldWorkOrderScope) -> WorkOrder:
        if (
            result.actor_revoked
            or query.actor != actor
            or query.public_id != work_order.public_id
        ):
            raise DomainError(
                code="operations.field_work_order_access.denied",
                message="Denied",
                retryable=False,
            )
        return work_order

    def assignment_gate(
        _db: Session, *, query: CurrentWorkOrderAssignmentQuery
    ) -> CurrentWorkOrderAssignment | None:
        if query.work_order_id != work_order.id:
            return None
        return CurrentWorkOrderAssignment(
            queue_id=result.issuance,
            technician_id=None,
            vendor_id=actor.native_vendor_id,
            generation=result.generation,
            effective_at=None,
            vendor_user_id=actor.vendor_user_id,
        )

    monkeypatch.setattr(owner, "require_work_order", visit_gate)
    monkeypatch.setattr(
        owner.work_order_commands, "current_assignment", assignment_gate
    )
    monkeypatch.setattr(owner, "revalidate_staff_admission", staff_gate)
    monkeypatch.setattr(
        staff_permission_admission, "revalidate_staff_admission", staff_gate
    )
    monkeypatch.setattr(owner, "resolve_field_actor", resolve_actor)
    monkeypatch.setattr(owner, "engineering_plan_revocation_scope", historical_gate)
    monkeypatch.setattr(owner, "_field_admit", field_gate)
    monkeypatch.setattr(owner, "emit_event", event_gate)
    monkeypatch.setattr(owner.audit_adapter, "stage", audit_gate)
    monkeypatch.setattr(
        project_engineering_plans, "stage_current_for_completion", engineering_gate
    )
    monkeypatch.setattr(
        project_engineering_plans, "resolve_current_for_completion", engineering_gate
    )
    monkeypatch.setattr(
        project_engineering_plans,
        "resolve_current_for_completion_review",
        engineering_gate,
    )
    configured = owner.publish_policy(
        db,
        command=PublishCompletionReviewPolicy(
            context=context(staff, WRITE),
            access=access(staff),
            scope=CompletionPolicyScope(
                kind=CompletionPolicyScopeKind.project, project_id=project
            ),
            expected_generation=0,
            independent_reviewer_required=True,
        ),
    )
    assert configured.policy_revision_id is not None
    result.policy = configured.policy_revision_id
    enrolled = owner.enroll(
        db,
        command=EnrollCompletionReview(
            context=context(staff, WRITE),
            access=access(staff),
            work_order_public_id="sub-completion-test",
            provenance=CompletionProvenance.native_work_order
            if native
            else CompletionProvenance.engineering_issuance,
            engineering_issuance_id=None if native else issuance,
            criteria=tuple(
                IssuedCompletionCriterion(
                    id=value,
                    order=order,
                    statement="Native operator criterion",
                    verification_method="Inspect native installation",
                )
                for order, value in enumerate(criteria)
            )
            if native
            else (),
            policy_scope=CompletionPolicyScope(
                kind=CompletionPolicyScopeKind.project, project_id=project
            ),
        ),
    )
    result.enrollment = enrolled.enrollment_id
    result.criteria_revision = enrolled.criteria_revision_id
    event = FieldJobEvent(
        id=uuid4(),
        work_order_mirror_id=work_order.id,
        author_vendor_user_id=actor.vendor_user_id,
        system_user_id=technician,
        event="complete",
        new_status="completed",
        assignment_generation=generation,
        occurred_at=datetime(2026, 10, 11, tzinfo=UTC),
        received_at=datetime(2026, 10, 11, tzinfo=UTC),
        client_event_id=uuid4(),
    )
    work_order.status = "completed"
    db.add(event)
    db.commit()
    result.completed_event = event.id
    db.rollback()
    yield result
    db.close()
    engine.dispose()


def submission(rig: Rig) -> SubmitCompletionEvidence:
    assert (
        rig.enrollment is not None
        and rig.criteria_revision is not None
        and rig.completed_event is not None
    )
    return SubmitCompletionEvidence(
        context=context(rig.field, "field:completion:submit"),
        requester_system_user_id=rig.field,
        enrollment_id=rig.enrollment,
        criteria_revision_id=rig.criteria_revision,
        completed_event_id=rig.completed_event,
        assignment_generation=rig.generation,
        expected_generation=0,
        policy_revision_id=rig.policy,
        observations=tuple(
            CriterionObservation(value, "Observed approved installation")
            for value in rig.criteria
        ),
    )


def decision(rig: Rig, submission_id: UUID) -> DecideCompletionReview:
    return DecideCompletionReview(
        context=context(rig.staff, WRITE),
        access=access(rig.staff),
        enrollment_id=rig.enrollment,
        submission_id=submission_id,
        expected_generation=1,
        policy_revision_id=rig.policy,
        results=tuple(
            CriterionResult(
                value, CriterionVerdict.passed, "Inspected approved evidence"
            )
            for value in rig.criteria
        ),
    )


def test_exact_criteria_acceptance_replay_history_and_revoke(rig: Rig) -> None:
    command = submission(rig)
    submitted = owner.submit_evidence(rig.db, command=command)
    assert submitted.submission_id is not None
    assert owner.submit_evidence(rig.db, command=command).replayed
    review = decision(rig, submitted.submission_id)
    accepted = owner.decide(rig.db, command=review)
    assert accepted.state == ReviewState.accepted
    assert owner.decide(rig.db, command=review).replayed
    assert len(rig.events) == 4 and len(rig.audits) == 4
    assert accepted.decision_id is not None
    revoked = owner.revoke(
        rig.db,
        command=RevokeCompletionReview(
            context=context(rig.staff, WRITE),
            access=access(rig.staff),
            enrollment_id=rig.enrollment,
            decision_id=accepted.decision_id,
            expected_generation=2,
        ),
    )
    assert revoked.state == ReviewState.revoked
    history = owner.history(
        rig.db,
        query=CompletionReviewQuery(
            access=access(rig.staff), enrollment_id=rig.enrollment
        ),
    )
    assert tuple(value.state for value in history.decisions) == (
        ReviewState.accepted,
        ReviewState.revoked,
    )


@pytest.mark.parametrize("mode", ("missing", "duplicate", "unknown"))
def test_submission_requires_every_issued_criterion_once(rig: Rig, mode: str) -> None:
    command = submission(rig)
    observations = (
        command.observations[:1]
        if mode == "missing"
        else (command.observations[0], command.observations[0])
        if mode == "duplicate"
        else (command.observations[0], CriterionObservation(uuid4(), "Unknown"))
    )
    with pytest.raises(DomainError, match="criterion mismatch"):
        owner.submit_evidence(
            rig.db, command=replace(command, observations=observations)
        )
    assert rig.db.scalar(select(func.count()).select_from(CompletionSubmission)) == 0


def test_reused_global_key_with_different_action_or_payload_conflicts(rig: Rig) -> None:
    command = submission(rig)
    owner.submit_evidence(rig.db, command=command)
    with pytest.raises(DomainError, match="idempotency conflict"):
        owner.submit_evidence(
            rig.db,
            command=replace(
                command,
                observations=tuple(
                    CriterionObservation(value, "Changed") for value in rig.criteria
                ),
            ),
        )


def test_revoked_assignment_denies_historical_submission_replay(rig: Rig) -> None:
    command = submission(rig)
    owner.submit_evidence(rig.db, command=command)
    rig.actor_revoked = True
    with pytest.raises(DomainError, match="assignment generation mismatch"):
        owner.submit_evidence(rig.db, command=command)


def test_engineering_revocation_blocks_decision_but_not_observation_or_revoke(
    rig: Rig,
) -> None:
    rig.engineering_stale = True
    result = owner.submit_evidence(rig.db, command=submission(rig))
    assert result.submission_id is not None
    with pytest.raises(DomainError, match="Unapproved"):
        owner.decide(rig.db, command=decision(rig, result.submission_id))
    rig.engineering_stale = False
    accepted = owner.decide(rig.db, command=decision(rig, result.submission_id))
    rig.engineering_stale = True
    assert (
        owner.current(
            rig.db,
            query=CompletionReviewQuery(
                access=access(rig.staff), enrollment_id=rig.enrollment
            ),
        ).state
        == ReviewState.stale
    )
    rig.db.rollback()  # queries own no business transaction
    assert accepted.decision_id is not None
    assert (
        owner.revoke(
            rig.db,
            command=RevokeCompletionReview(
                context=context(rig.staff, WRITE),
                access=access(rig.staff),
                enrollment_id=rig.enrollment,
                decision_id=accepted.decision_id,
                expected_generation=2,
            ),
        ).state
        == ReviewState.revoked
    )


def test_staff_permission_revocation_denies_decision_replay(rig: Rig) -> None:
    result = owner.submit_evidence(rig.db, command=submission(rig))
    assert result.submission_id is not None
    command = decision(rig, result.submission_id)
    owner.decide(rig.db, command=command)
    rig.denied = True
    with pytest.raises(DomainError, match="Denied"):
        owner.decide(rig.db, command=command)


def test_failed_criterion_rejects_and_missing_result_denies(rig: Rig) -> None:
    result = owner.submit_evidence(rig.db, command=submission(rig))
    assert result.submission_id is not None
    command = decision(rig, result.submission_id)
    with pytest.raises(DomainError, match="criterion mismatch"):
        owner.decide(rig.db, command=replace(command, results=command.results[:1]))
    command = replace(
        command,
        results=(
            replace(command.results[0], verdict=CriterionVerdict.failed),
            command.results[1],
        ),
    )
    assert owner.decide(rig.db, command=command).state == ReviewState.rejected


def test_outbox_failure_rolls_back_observations_head_and_receipt(
    rig: Rig, monkeypatch: pytest.MonkeyPatch
) -> None:
    def fail(
        _db: Session,
        event_type: EventType,
        payload: dict[str, object],
        *,
        actor: str | None,
    ) -> Event:
        raise RuntimeError("Injected outbox failure")

    monkeypatch.setattr(owner, "emit_event", fail)
    with pytest.raises(RuntimeError, match="outbox failure"):
        owner.submit_evidence(rig.db, command=submission(rig))
    assert rig.db.scalar(select(func.count()).select_from(CompletionSubmission)) == 0
    assert rig.db.scalar(select(func.count()).select_from(CompletionReviewHead)) == 0


def test_legacy_completion_is_not_accepted(rig: Rig) -> None:
    result = owner.current(
        rig.db,
        query=CompletionReviewQuery(
            access=access(rig.staff), enrollment_id=rig.enrollment
        ),
    )
    assert result.state == ReviewState.legacy_not_reviewed and result.generation == 0


def test_independent_reviewer_policy_is_explicit_and_enforced(rig: Rig) -> None:
    result = owner.submit_evidence(rig.db, command=submission(rig))
    assert result.submission_id is not None
    command = decision(rig, result.submission_id)
    command = replace(
        command, access=access(rig.field), context=context(rig.field, WRITE)
    )
    with pytest.raises(DomainError, match="independent reviewer required"):
        owner.decide(rig.db, command=command)


def test_evidence_retention_blocks_direct_soft_delete_and_physical_alias(
    rig: Rig, monkeypatch: pytest.MonkeyPatch
) -> None:
    from app.models.stored_file_retention import StoredFileRetention
    from app.services.file_storage import file_uploads

    command = submission(rig)
    issuance = rig.db.get(ProjectWorkOrderIssuance, rig.issuance)
    assert issuance is not None
    file = StoredFile(
        id=uuid4(),
        entity_type="field_attachment",
        entity_id=issuance.public_id,
        original_filename="proof.pdf",
        storage_key_or_relative_path="attachments/shared-proof",
        file_size=5,
        content_type="application/pdf",
        checksum=hashlib.sha256(b"proof").hexdigest(),
        storage_provider="s3",
    )
    alias = StoredFile(
        id=uuid4(),
        entity_type="field_attachment",
        entity_id=issuance.public_id,
        original_filename="proof.pdf",
        storage_key_or_relative_path=file.storage_key_or_relative_path,
        file_size=5,
        content_type="application/pdf",
        checksum=hashlib.sha256(b"proof").hexdigest(),
        storage_provider="s3",
    )
    attachment = FieldAttachment(
        id=uuid4(),
        work_order_mirror_id=issuance.work_order_id,
        stored_file_id=file.id,
        kind="document",
        file_name="proof.pdf",
        mime_type="application/pdf",
        size_bytes=5,
        uploaded_by_person_id=uuid4(),
    )
    rig.db.add_all([file, alias, attachment])
    rig.db.commit()
    command = replace(
        command,
        observations=(
            replace(command.observations[0], attachment_ids=(attachment.id,)),
            command.observations[1],
        ),
    )
    rig.db.rollback()  # Release expired-identity observation after fixture commit.
    result = owner.submit_evidence(rig.db, command=command)
    assert rig.db.get(StoredFileRetention, (file.id, result.submission_id)) is not None
    rig.db.rollback()

    class Storage:
        def delete(self, key: str) -> None:
            pytest.fail("Retained physical object must never reach deletion")

    monkeypatch.setattr(file_uploads, "storage", Storage())
    for record in (file, alias):
        with pytest.raises(DomainError, match="must be retained"):
            file_uploads.soft_delete(db=rig.db, file=record, hard_delete_object=True)
        rig.db.rollback()
        with pytest.raises(DomainError, match="must be retained"):
            file_uploads.stage_soft_delete(db=rig.db, file=record)
        rig.db.rollback()


def test_hard_delete_preserves_object_while_another_active_alias_exists(
    rig: Rig, monkeypatch: pytest.MonkeyPatch
) -> None:
    from app.services.file_storage import file_uploads

    files = tuple(
        StoredFile(
            entity_type="field_attachment",
            entity_id="shared",
            original_filename="proof.pdf",
            storage_key_or_relative_path="attachments/unretained-shared",
            file_size=4,
            storage_provider="s3",
        )
        for _ in range(2)
    )
    rig.db.add_all(files)
    rig.db.commit()
    calls: list[str] = []

    class Storage:
        def delete(self, key: str) -> None:
            calls.append(key)

    monkeypatch.setattr(file_uploads, "storage", Storage())
    file_uploads.soft_delete(db=rig.db, file=files[0], hard_delete_object=True)
    assert not calls
    file_uploads.soft_delete(db=rig.db, file=files[1], hard_delete_object=True)
    assert calls == ["attachments/unretained-shared"]


def test_prepared_upload_deleted_before_staging_fails_closed(
    rig: Rig, monkeypatch: pytest.MonkeyPatch
) -> None:
    from app.services.file_storage import PreparedFileUpload, file_uploads
    from app.services.object_storage import ObjectStorageError

    prepared = PreparedFileUpload(
        owner_subscriber_id=None,
        entity_type="field_attachment",
        entity_id="scope",
        original_filename="proof.pdf",
        storage_key="attachments/prepared-deleted",
        file_size=4,
        content_type="application/pdf",
        checksum="a" * 64,
        uploaded_by=None,
    )

    class MissingStorage:
        def exists(self, key: str) -> bool:
            return False

    monkeypatch.setattr(file_uploads, "storage", MissingStorage())
    with pytest.raises(DomainError) as missing:
        file_uploads.stage_prepared_upload(db=rig.db, prepared=prepared)
    assert (
        missing.value.code == "files.prepared_object_missing"
        and missing.value.retryable
    )
    assert (
        rig.db.scalar(
            select(StoredFile.id).where(
                StoredFile.storage_key_or_relative_path == prepared.storage_key
            )
        )
        is None
    )
    rig.db.rollback()

    class UnavailableStorage:
        def exists(self, key: str) -> bool:
            raise ObjectStorageError("Synthetic transport failure")

    monkeypatch.setattr(file_uploads, "storage", UnavailableStorage())
    with pytest.raises(DomainError) as unavailable:
        file_uploads.stage_prepared_upload(db=rig.db, prepared=prepared)
    assert (
        unavailable.value.code == "files.storage_unavailable"
        and unavailable.value.retryable
    )


def test_policy_can_explicitly_allow_same_person_review(rig: Rig) -> None:
    configured = owner.publish_policy(
        rig.db,
        command=PublishCompletionReviewPolicy(
            context=context(rig.staff, WRITE),
            access=access(rig.staff),
            scope=CompletionPolicyScope(
                kind=CompletionPolicyScopeKind.project, project_id=rig.project
            ),
            expected_generation=1,
            independent_reviewer_required=False,
        ),
    )
    assert configured.policy_revision_id is not None
    rig.policy = configured.policy_revision_id
    submitted = owner.submit_evidence(rig.db, command=submission(rig))
    assert submitted.submission_id is not None
    command = decision(rig, submitted.submission_id)
    command = replace(
        command, access=access(rig.field), context=context(rig.field, WRITE)
    )
    assert owner.decide(rig.db, command=command).state == ReviewState.accepted


def test_current_refreshes_head_after_project_lock_wait(
    rig: Rig, monkeypatch: pytest.MonkeyPatch
) -> None:
    from sqlalchemy import update

    submitted = owner.submit_evidence(rig.db, command=submission(rig))
    assert submitted.submission_id is not None
    owner.decide(rig.db, command=decision(rig, submitted.submission_id))

    def after_wait(
        db: Session, *, project_id: UUID, task_id: UUID | None
    ) -> ProjectEngineeringRevocationScope:
        db.execute(
            update(CompletionReviewHead)
            .where(CompletionReviewHead.enrollment_id == rig.enrollment)
            .values(generation=3, decision_id=None)
            .execution_options(synchronize_session=False)
        )
        return historical_scope(project_id, task_id)

    monkeypatch.setattr(owner, "engineering_plan_revocation_scope", after_wait)
    result = owner.current(
        rig.db,
        query=CompletionReviewQuery(
            access=access(rig.staff), enrollment_id=rig.enrollment
        ),
    )
    assert (
        result.state == ReviewState.submitted
        and result.generation == 3
        and result.decision_id is None
    )


def test_current_revalidates_read_permission_after_lock_wait(
    rig: Rig, monkeypatch: pytest.MonkeyPatch
) -> None:
    def after_wait(
        db: Session, *, project_id: UUID, task_id: UUID | None
    ) -> ProjectEngineeringRevocationScope:
        rig.denied = True
        return historical_scope(project_id, task_id)

    monkeypatch.setattr(owner, "engineering_plan_revocation_scope", after_wait)
    with pytest.raises(DomainError, match="Denied"):
        owner.current(
            rig.db,
            query=CompletionReviewQuery(
                access=access(rig.staff), enrollment_id=rig.enrollment
            ),
        )


def test_current_marks_retargeted_work_order_acceptance_stale(rig: Rig) -> None:
    submitted = owner.submit_evidence(rig.db, command=submission(rig))
    assert submitted.submission_id is not None
    owner.decide(rig.db, command=decision(rig, submitted.submission_id))
    issuance = rig.db.get(ProjectWorkOrderIssuance, rig.issuance)
    assert issuance is not None
    work_order = rig.db.get(WorkOrder, issuance.work_order_id)
    assert work_order is not None
    work_order.project_id = uuid4()
    rig.db.commit()
    assert (
        owner.current(
            rig.db,
            query=CompletionReviewQuery(
                access=access(rig.staff), enrollment_id=rig.enrollment
            ),
        ).state
        == ReviewState.stale
    )


def test_submission_replay_uses_historical_origin_scope(
    rig: Rig, monkeypatch: pytest.MonkeyPatch
) -> None:
    command = submission(rig)
    submitted = owner.submit_evidence(rig.db, command=command)

    def active_origin_forbidden(
        db: Session, *, project_id: UUID, task_id: UUID | None, lock: bool
    ) -> ProjectEngineeringOrigin:
        raise DomainError(
            code="operations.project_lifecycle.relationship_conflict",
            message="Archived origin",
        )

    monkeypatch.setattr(
        "app.services.projects.engineering_plan_origin", active_origin_forbidden
    )
    replay = owner.submit_evidence(rig.db, command=command)
    assert replay.replayed and replay.submission_id == submitted.submission_id


def test_active_file_metadata_cannot_retain_missing_physical_object(
    rig: Rig, monkeypatch: pytest.MonkeyPatch
) -> None:
    from app.services.file_storage import (
        CompletionFileRetentionRegistration,
        stage_completion_retention,
    )
    from app.services.object_storage import ObjectNotFoundError
    from app.services.owner_commands import (
        OwnerCommandDefinition,
        execute_owner_command,
    )

    file = StoredFile(
        entity_type="field_attachment",
        entity_id="scope",
        original_filename="missing.pdf",
        storage_key_or_relative_path="attachments/missing-physical",
        file_size=5,
        storage_provider="s3",
        checksum=hashlib.sha256(b"proof").hexdigest(),
    )
    rig.db.add(file)
    rig.db.flush()
    file_id = file.id
    rig.db.commit()

    class MissingStorage:
        def stream(self, key: str) -> StreamResult:
            raise ObjectNotFoundError("Synthetic absent bytes")

    monkeypatch.setattr(file_uploads, "storage", MissingStorage())
    with pytest.raises(DomainError) as missing:
        execute_owner_command(
            rig.db,
            definition=OwnerCommandDefinition(
                owner=owner.OWNER,
                concern=owner.CONCERN,
                name="retain_synthetic_evidence",
            ),
            context=context(rig.field, "field:completion:submit"),
            operation=lambda: stage_completion_retention(
                rig.db, command=CompletionFileRetentionRegistration(file_id, uuid4())
            ),
        )
    assert (
        missing.value.code == "files.evidence_unavailable"
        and not missing.value.retryable
    )
    assert (
        rig.db.scalar(
            select(StoredFileRetention).where(
                StoredFileRetention.stored_file_id == file_id
            )
        )
        is None
    )


@pytest.mark.parametrize("independent", [True, False])
def test_missing_submitter_person_preserves_evidence_and_respects_policy(
    rig: Rig, independent: bool
) -> None:
    user = rig.db.get(SystemUser, rig.field)
    assert user is not None
    user.person_party_id = None
    user.party_bound_at = None
    user.party_binding_source = None
    user.party_binding_reason = None
    rig.db.commit()
    if not independent:
        policy = owner.publish_policy(
            rig.db,
            command=PublishCompletionReviewPolicy(
                context=context(rig.staff, WRITE),
                access=access(rig.staff),
                scope=CompletionPolicyScope(
                    kind=CompletionPolicyScopeKind.project, project_id=rig.project
                ),
                expected_generation=1,
                independent_reviewer_required=False,
            ),
        )
        assert policy.policy_revision_id is not None
        rig.policy = policy.policy_revision_id
    submitted = owner.submit_evidence(rig.db, command=submission(rig))
    assert submitted.submission_id is not None
    stored = rig.db.get(CompletionSubmission, submitted.submission_id)
    assert stored is not None and stored.actor_person_id is None
    rig.db.rollback()
    if independent:
        with pytest.raises(DomainError) as denied:
            owner.decide(rig.db, command=decision(rig, submitted.submission_id))
        assert (
            denied.value.code
            == "operations.field_completion.independent_reviewer_required"
        )
        assert not denied.value.retryable
        assert (
            rig.db.scalar(select(func.count()).select_from(CompletionReviewDecision))
            == 0
        )
        assert (
            rig.db.scalar(select(func.count()).select_from(CompletionSubmission)) == 1
        )
    else:
        assert (
            owner.decide(rig.db, command=decision(rig, submitted.submission_id)).state
            == ReviewState.accepted
        )


def test_independent_review_denies_distinct_accounts_for_same_canonical_person(
    rig: Rig,
) -> None:
    user = rig.db.get(SystemUser, rig.field)
    assert user is not None and user.id != rig.staff
    user.person_party_id = rig.person
    rig.db.commit()
    submitted = owner.submit_evidence(rig.db, command=submission(rig))
    assert submitted.submission_id is not None
    with pytest.raises(DomainError, match="independent reviewer required"):
        owner.decide(rig.db, command=decision(rig, submitted.submission_id))


def public_id(rig: Rig) -> str:
    issuance = rig.db.get(ProjectWorkOrderIssuance, rig.issuance)
    assert issuance is not None
    return issuance.public_id


def test_field_preview_discovers_exact_issued_criteria_and_current_assignment(
    rig: Rig,
) -> None:
    rig.db.add(
        EngineeringPlanCriterion(
            revision_id=uuid4(),
            id=uuid4(),
            order=0,
            statement="Unissued latest scope",
            verification_method="Never substitute",
        )
    )
    rig.db.commit()
    rig.engineering_stale = True
    preview = owner.field_submission_preview(
        rig.db,
        query=FieldSubmissionPreviewQuery(
            requester_system_user_id=rig.field, work_order_public_id=public_id(rig)
        ),
    )
    assert preview.enrollment_id == rig.enrollment
    assert preview.assignment_generation == rig.generation
    assert preview.policy_revision_id == rig.policy and preview.generation == 0
    assert {criterion.id for criterion in preview.criteria} == set(rig.criteria)
    assert all(
        criterion.statement != "Unissued latest scope" for criterion in preview.criteria
    )
    assert (
        preview.submission_available and preview.submission_unavailable_reason is None
    )
    assert preview.submission_readiness.action_key == "field_completion.submit_evidence"


def test_field_preview_denies_removed_assignment_before_returning_history(
    rig: Rig,
) -> None:
    owner.submit_evidence(rig.db, command=submission(rig))
    rig.actor_revoked = True
    with pytest.raises(DomainError, match="Denied"):
        owner.field_submission_preview(
            rig.db,
            query=FieldSubmissionPreviewQuery(
                requester_system_user_id=rig.field, work_order_public_id=public_id(rig)
            ),
        )


def test_field_preview_policy_absence_is_actionable_not_implicit_enrollment(
    rig: Rig,
) -> None:
    head = rig.db.get(CompletionReviewPolicyHead, ("project", rig.project))
    assert head is not None
    rig.db.delete(head)
    rig.db.commit()
    preview = owner.field_submission_preview(
        rig.db,
        query=FieldSubmissionPreviewQuery(
            requester_system_user_id=rig.field, work_order_public_id=public_id(rig)
        ),
    )
    assert not preview.submission_available and preview.policy_revision_id is None
    assert preview.submission_readiness.blockers[0].code.endswith("policy_required")


def test_decide_workspace_write_purpose_has_no_read_floor_or_broad_history(
    rig: Rig,
) -> None:
    submitted = owner.submit_evidence(rig.db, command=submission(rig))
    write_only = PermissionAdmission(rig.staff, "system_user", frozenset({WRITE}))
    workspace = owner.workspace(
        rig.db,
        query=CompletionWorkspaceQuery(
            access=write_only,
            work_order_public_id=public_id(rig),
            purpose=CompletionWorkspacePurpose.decide,
        ),
    )
    assert workspace.decide_readiness.is_ready
    assert workspace.decide_readiness.action_key == "field_completion.decide"
    assert workspace.history is None and workspace.current_submission is not None
    assert workspace.current_submission.id == submitted.submission_id
    assert workspace.policy is not None and workspace.policy.generation == 1
    with pytest.raises(DomainError, match="Denied"):
        owner.workspace(
            rig.db,
            query=CompletionWorkspaceQuery(
                access=write_only, work_order_public_id=public_id(rig)
            ),
        )


def test_read_workspace_history_does_not_grant_write_preview(rig: Rig) -> None:
    owner.submit_evidence(rig.db, command=submission(rig))
    read_only = PermissionAdmission(rig.staff, "system_user", frozenset({READ}))
    workspace = owner.workspace(
        rig.db,
        query=CompletionWorkspaceQuery(
            access=read_only, work_order_public_id=public_id(rig)
        ),
    )
    assert workspace.history is not None and len(workspace.history.submissions) == 1
    assert not workspace.decide_readiness.is_ready
    assert workspace.decide_readiness.blockers[0].code.endswith(
        "write_preview_required"
    )
    with pytest.raises(DomainError, match="Denied"):
        owner.workspace(
            rig.db,
            query=CompletionWorkspaceQuery(
                access=read_only,
                work_order_public_id=public_id(rig),
                purpose=CompletionWorkspacePurpose.decide,
            ),
        )


def test_policy_publication_preview_is_explicit_write_purpose(rig: Rig) -> None:
    snapshot = owner.policy_snapshot(
        rig.db,
        query=CompletionPolicyQuery(
            access=PermissionAdmission(rig.staff, "system_user", frozenset({WRITE})),
            scope=CompletionPolicyScope(
                kind=CompletionPolicyScopeKind.project, project_id=rig.project
            ),
            purpose=CompletionPolicyPurpose.publish_policy,
        ),
    )
    assert snapshot.revision_id == rig.policy and snapshot.generation == 1
    assert snapshot.independent_reviewer_required and snapshot.reason
    assert snapshot.publish_readiness.is_ready
    assert rig.db.scalar(select(func.count()).select_from(CompletionReviewPolicy)) == 1


def test_decide_preview_blocks_stale_engineering_and_missing_person(rig: Rig) -> None:
    user = rig.db.get(SystemUser, rig.field)
    assert user is not None
    user.person_party_id = None
    user.party_bound_at = user.party_binding_source = user.party_binding_reason = None
    rig.db.commit()
    owner.submit_evidence(rig.db, command=submission(rig))
    query = CompletionWorkspaceQuery(
        access=access(rig.staff),
        work_order_public_id=public_id(rig),
        purpose=CompletionWorkspacePurpose.decide,
    )
    assert (
        owner.workspace(rig.db, query=query)
        .decide_readiness.blockers[0]
        .code.endswith("independent_reviewer_required")
    )
    rig.engineering_stale = True
    assert (
        owner.workspace(rig.db, query=query)
        .decide_readiness.blockers[0]
        .code.endswith("engineering_stale")
    )


def test_revoke_preview_does_not_require_current_engineering(
    rig: Rig, monkeypatch: pytest.MonkeyPatch
) -> None:
    submitted = owner.submit_evidence(rig.db, command=submission(rig))
    assert submitted.submission_id is not None
    owner.decide(rig.db, command=decision(rig, submitted.submission_id))

    def forbidden(
        _db: Session, *, query: CompletionEngineeringPlanQuery
    ) -> CurrentEngineeringPlanApproval:
        pytest.fail("Withdrawal must not require current engineering approval")

    monkeypatch.setattr(
        project_engineering_plans, "resolve_current_for_completion_review", forbidden
    )
    result = owner.workspace(
        rig.db,
        query=CompletionWorkspaceQuery(
            access=access(rig.staff),
            work_order_public_id=public_id(rig),
            purpose=CompletionWorkspacePurpose.revoke,
        ),
    )
    assert result.revoke_readiness.is_ready and result.current_decision is not None
    assert result.history is None


def test_route_public_id_mismatch_denies_submission_and_receipt_replay(
    rig: Rig,
) -> None:
    command = submission(rig)
    owner.submit_evidence(rig.db, command=command)
    with pytest.raises(DomainError, match="scope mismatch"):
        owner.submit_evidence(
            rig.db,
            command=replace(command, expected_work_order_public_id="other-visit"),
        )


def retained_submission(rig: Rig) -> tuple[UUID, UUID]:
    issuance = rig.db.get(ProjectWorkOrderIssuance, rig.issuance)
    assert issuance is not None
    file = StoredFile(
        id=uuid4(),
        entity_type="field_attachment",
        entity_id=issuance.public_id,
        original_filename="retained-proof.pdf",
        storage_key_or_relative_path="attachments/retained-proof",
        file_size=5,
        content_type="application/pdf",
        checksum=hashlib.sha256(b"proof").hexdigest(),
        storage_provider="s3",
    )
    attachment = FieldAttachment(
        id=uuid4(),
        work_order_mirror_id=issuance.work_order_id,
        stored_file_id=file.id,
        kind="document",
        file_name="retained-proof.pdf",
        mime_type="application/pdf",
        size_bytes=5,
        uploaded_by_person_id=uuid4(),
    )
    rig.db.add_all([file, attachment])
    rig.db.commit()
    attachment_id = attachment.id
    command = submission(rig)
    command = replace(
        command,
        observations=(
            replace(command.observations[0], attachment_ids=(attachment_id,)),
            *command.observations[1:],
        ),
    )
    rig.db.rollback()
    result = owner.submit_evidence(rig.db, command=command)
    assert result.submission_id is not None
    return result.submission_id, attachment_id


def test_retained_evidence_read_requires_exact_lineage_and_permission(rig: Rig) -> None:
    submitted, attachment = retained_submission(rig)
    query = CompletionEvidenceQuery(
        access=access(rig.staff),
        enrollment_id=rig.enrollment,
        submission_id=submitted,
        attachment_id=attachment,
    )
    result = owner.evidence_content(rig.db, query=query)
    assert result.evidence.filename == "retained-proof.pdf"
    assert b"".join(result.stream.chunks) == b"proof"
    assert result.evidence.checksum == hashlib.sha256(b"proof").hexdigest()
    with pytest.raises(DomainError, match="not found"):
        owner.evidence_content(rig.db, query=replace(query, submission_id=uuid4()))
    rig.denied = True
    with pytest.raises(DomainError, match="Denied"):
        owner.evidence_content(rig.db, query=query)


def test_write_evidence_preview_only_exposes_current_pending_submission(
    rig: Rig,
) -> None:
    submitted, attachment = retained_submission(rig)
    query = CompletionEvidenceQuery(
        access=PermissionAdmission(rig.staff, "system_user", frozenset({WRITE})),
        enrollment_id=rig.enrollment,
        submission_id=submitted,
        attachment_id=attachment,
        purpose=CompletionWorkspacePurpose.decide,
    )
    assert (
        b"".join(owner.evidence_content(rig.db, query=query).stream.chunks) == b"proof"
    )
    rig.db.rollback()
    owner.decide(rig.db, command=decision(rig, submitted))
    with pytest.raises(DomainError, match="denied"):
        owner.evidence_content(rig.db, query=query)
    read_query = replace(
        query, access=access(rig.staff), purpose=CompletionWorkspacePurpose.read
    )
    assert (
        b"".join(owner.evidence_content(rig.db, query=read_query).stream.chunks)
        == b"proof"
    )


def test_retained_evidence_never_returns_tampered_bytes(
    rig: Rig, monkeypatch: pytest.MonkeyPatch
) -> None:
    submitted, attachment = retained_submission(rig)

    class TamperedStorage:
        def stream(self, key: str) -> StreamResult:
            return StreamResult(
                chunks=iter((b"wrong",)),
                content_type="application/pdf",
                content_length=5,
            )

    monkeypatch.setattr(file_uploads, "storage", TamperedStorage())
    with pytest.raises(DomainError) as denied:
        owner.evidence_content(
            rig.db,
            query=CompletionEvidenceQuery(
                access=access(rig.staff),
                enrollment_id=rig.enrollment,
                submission_id=submitted,
                attachment_id=attachment,
            ),
        )
    assert (
        denied.value.code == "files.evidence_unavailable" and not denied.value.retryable
    )


def test_staff_route_public_id_mismatch_denies_review_and_replay(rig: Rig) -> None:
    submitted = owner.submit_evidence(rig.db, command=submission(rig))
    assert submitted.submission_id is not None
    command = decision(rig, submitted.submission_id)
    with pytest.raises(DomainError, match="scope mismatch"):
        owner.decide(
            rig.db,
            command=replace(command, expected_work_order_public_id="other-visit"),
        )
    accepted = owner.decide(rig.db, command=command)
    assert accepted.decision_id is not None
    with pytest.raises(DomainError, match="scope mismatch"):
        owner.decide(
            rig.db,
            command=replace(command, expected_work_order_public_id="other-visit"),
        )
    withdrawal = RevokeCompletionReview(
        context=context(rig.staff, WRITE),
        access=access(rig.staff),
        enrollment_id=rig.enrollment,
        decision_id=accepted.decision_id,
        expected_generation=2,
        expected_work_order_public_id="other-visit",
    )
    with pytest.raises(DomainError, match="scope mismatch"):
        owner.revoke(rig.db, command=withdrawal)


def test_retained_evidence_revalidates_admission_after_provider_wait(
    rig: Rig, monkeypatch: pytest.MonkeyPatch
) -> None:
    submitted, attachment = retained_submission(rig)

    class PermissionRevokingStorage:
        def stream(self, key: str) -> StreamResult:
            rig.denied = True
            return StreamResult(
                chunks=iter((b"proof",)),
                content_type="application/pdf",
                content_length=5,
            )

    monkeypatch.setattr(file_uploads, "storage", PermissionRevokingStorage())
    with pytest.raises(DomainError, match="Denied"):
        owner.evidence_content(
            rig.db,
            query=CompletionEvidenceQuery(
                access=access(rig.staff),
                enrollment_id=rig.enrollment,
                submission_id=submitted,
                attachment_id=attachment,
            ),
        )


def test_workspace_revalidates_admission_after_assignment_read_wait(
    rig: Rig, monkeypatch: pytest.MonkeyPatch
) -> None:
    owner.submit_evidence(rig.db, command=submission(rig))

    def after_wait(
        _db: Session, *, query: CurrentWorkOrderAssignmentQuery
    ) -> CurrentWorkOrderAssignment | None:
        rig.denied = True
        return None

    monkeypatch.setattr(owner.work_order_commands, "current_assignment", after_wait)
    with pytest.raises(DomainError, match="Denied"):
        owner.workspace(
            rig.db,
            query=CompletionWorkspaceQuery(
                access=access(rig.staff), work_order_public_id=public_id(rig)
            ),
        )


@pytest.mark.parametrize("rig", ["native"], indirect=True)
def test_native_work_order_has_real_enrollment_without_engineering(rig: Rig) -> None:
    command = submission(rig)
    submitted = owner.submit_evidence(rig.db, command=command)
    assert submitted.submission_id is not None
    accepted = owner.decide(rig.db, command=decision(rig, submitted.submission_id))
    assert accepted.state == ReviewState.accepted
    assert accepted.provenance == CompletionProvenance.native_work_order
    assert accepted.engineering_issuance_id is None
    assert (
        rig.db.scalar(select(func.count()).select_from(ProjectWorkOrderIssuance)) == 0
    )
    rig.db.rollback()
    current = owner.current(
        rig.db,
        query=CompletionReviewQuery(
            access=access(rig.staff), enrollment_id=command.enrollment_id
        ),
    )
    assert current.state == ReviewState.accepted


@pytest.mark.parametrize("rig", ["native"], indirect=True)
def test_native_criteria_revision_invalidates_acceptance_and_preserves_history(
    rig: Rig,
) -> None:
    command = submission(rig)
    submitted = owner.submit_evidence(rig.db, command=command)
    assert submitted.submission_id is not None
    owner.decide(rig.db, command=decision(rig, submitted.submission_id))
    revised = owner.publish_criteria(
        rig.db,
        command=PublishCompletionCriteria(
            context=context(rig.staff, WRITE),
            access=access(rig.staff),
            enrollment_id=command.enrollment_id,
            expected_generation=1,
            criteria=tuple(
                IssuedCompletionCriterion(
                    id=value,
                    order=order,
                    statement="Changed native statement",
                    verification_method="Changed native method",
                )
                for order, value in enumerate(rig.criteria)
            ),
        ),
    )
    assert revised.criteria_revision_id != command.criteria_revision_id
    current = owner.current(
        rig.db,
        query=CompletionReviewQuery(
            access=access(rig.staff), enrollment_id=command.enrollment_id
        ),
    )
    assert current.state == ReviewState.stale
    history = owner.history(
        rig.db,
        query=CompletionReviewQuery(
            access=access(rig.staff), enrollment_id=command.enrollment_id
        ),
    )
    record = history.submissions[0]
    assert record.criteria_revision_id == command.criteria_revision_id
    assert {item.statement for item in record.criteria} == {"Native operator criterion"}
    assert {item.verification_method for item in record.criteria} == {
        "Inspect native installation"
    }
    rig.db.rollback()
    with pytest.raises(DomainError, match="criteria"):
        owner.submit_evidence(
            rig.db,
            command=replace(
                command,
                context=context(rig.field, "field:completion:submit"),
                expected_generation=2,
            ),
        )


@pytest.mark.parametrize("rig", ["native"], indirect=True)
def test_native_submission_requires_exact_current_completion_event(rig: Rig) -> None:
    command = submission(rig)
    with pytest.raises(DomainError) as refused:
        owner.submit_evidence(
            rig.db, command=replace(command, completed_event_id=uuid4())
        )
    assert refused.value.code.endswith("completion_required")
    assert rig.db.scalar(select(func.count()).select_from(CompletionSubmission)) == 0
    rig.db.rollback()
    submitted = owner.submit_evidence(rig.db, command=command)
    assert submitted.submission_id is not None
    owner.decide(rig.db, command=decision(rig, submitted.submission_id))
    prior = rig.db.get(FieldJobEvent, command.completed_event_id)
    assert prior is not None
    rig.db.add(
        FieldJobEvent(
            id=uuid4(),
            work_order_mirror_id=prior.work_order_mirror_id,
            author_vendor_user_id=prior.author_vendor_user_id,
            system_user_id=prior.system_user_id,
            event="complete",
            new_status="completed",
            assignment_generation=prior.assignment_generation,
            client_event_id=uuid4(),
            occurred_at=datetime(2026, 10, 12, tzinfo=UTC),
            received_at=datetime(2026, 10, 12, tzinfo=UTC),
        )
    )
    rig.db.commit()
    current = owner.current(
        rig.db,
        query=CompletionReviewQuery(
            access=access(rig.staff), enrollment_id=command.enrollment_id
        ),
    )
    assert current.state == ReviewState.stale


@pytest.mark.parametrize("status", ["closed", "resolved", "canceled"])
def test_terminal_native_origin_ticket_cannot_be_enrolled(
    rig: Rig, status: str
) -> None:
    ticket_id, work_order_id = uuid4(), uuid4()
    rig.db.add(
        Ticket(
            id=ticket_id,
            title="Terminal enrollment origin",
            status=status,
            is_active=True,
        )
    )
    rig.db.add(
        WorkOrder(
            id=work_order_id,
            public_id="terminal-native-enrollment",
            title="Native terminal origin",
            project_id=rig.project,
            origin_ticket_id=ticket_id,
        )
    )
    rig.db.commit()
    preview = owner.workspace(
        rig.db,
        query=CompletionWorkspaceQuery(
            access=access(rig.staff),
            work_order_public_id="terminal-native-enrollment",
            purpose=CompletionWorkspacePurpose.enroll,
        ),
    )
    assert not preview.enroll_readiness.is_ready
    assert any(
        blocker.code.endswith("origin_ticket_terminal")
        for blocker in preview.enroll_readiness.blockers
    )
    rig.db.rollback()
    with pytest.raises(DomainError) as refused:
        owner.enroll(
            rig.db,
            command=EnrollCompletionReview(
                context=context(rig.staff, WRITE),
                access=access(rig.staff),
                work_order_public_id="terminal-native-enrollment",
                provenance=CompletionProvenance.native_work_order,
                policy_scope=CompletionPolicyScope(
                    kind=CompletionPolicyScopeKind.project, project_id=rig.project
                ),
                criteria=(
                    IssuedCompletionCriterion(
                        id=uuid4(),
                        order=0,
                        statement="Inspect installation",
                        verification_method="Site inspection",
                    ),
                ),
            ),
        )
    assert refused.value.code.endswith("origin_ticket_terminal")
    assert (
        rig.db.scalar(
            select(CompletionEnrollment).where(
                CompletionEnrollment.work_order_id == work_order_id
            )
        )
        is None
    )


@pytest.mark.parametrize("rig", ["native"], indirect=True)
@pytest.mark.parametrize("drift", ["criteria", "completed_event"])
def test_withdrawal_receipt_pins_historical_submission_after_native_drift(
    rig: Rig, drift: str
) -> None:
    command = submission(rig)
    submitted = owner.submit_evidence(rig.db, command=command)
    assert submitted.submission_id is not None
    accepted = owner.decide(rig.db, command=decision(rig, submitted.submission_id))
    assert accepted.decision_id is not None
    if drift == "criteria":
        owner.publish_criteria(
            rig.db,
            command=PublishCompletionCriteria(
                context=context(rig.staff, WRITE),
                access=access(rig.staff),
                enrollment_id=command.enrollment_id,
                expected_generation=1,
                criteria=tuple(
                    IssuedCompletionCriterion(
                        id=value,
                        order=order,
                        statement="New criterion after acceptance",
                        verification_method="New method",
                    )
                    for order, value in enumerate(rig.criteria)
                ),
            ),
        )
    else:
        previous = rig.db.get(FieldJobEvent, command.completed_event_id)
        assert previous is not None
        rig.db.add(
            FieldJobEvent(
                work_order_mirror_id=previous.work_order_mirror_id,
                system_user_id=previous.system_user_id,
                author_vendor_user_id=previous.author_vendor_user_id,
                event="complete",
                new_status="completed",
                assignment_generation=previous.assignment_generation,
                client_event_id=uuid4(),
                occurred_at=datetime(2026, 10, 12, tzinfo=UTC),
                received_at=datetime(2026, 10, 12, tzinfo=UTC),
            )
        )
        rig.db.commit()
    withdrawal = RevokeCompletionReview(
        context=context(rig.staff, WRITE),
        access=access(rig.staff),
        enrollment_id=command.enrollment_id,
        decision_id=accepted.decision_id,
        expected_generation=accepted.generation,
    )
    result = owner.revoke(rig.db, command=withdrawal)
    assert result.state == ReviewState.revoked
    assert result.criteria_revision_id == command.criteria_revision_id
    assert result.completed_event_id == command.completed_event_id
    assert result.criteria_generation == 1
    replay = owner.revoke(rig.db, command=withdrawal)
    assert replay.replayed and replay.completed_event_id == command.completed_event_id
    assert replay.criteria_revision_id == command.criteria_revision_id
    receipt = rig.db.get(CompletionReviewReceipt, withdrawal.context.idempotency_key)
    assert (
        receipt is not None and receipt.completed_event_id == command.completed_event_id
    )


@pytest.mark.parametrize("rig", ["native"], indirect=True)
def test_publish_criteria_flushes_head_before_refresh_when_autoflush_disabled(
    rig: Rig,
) -> None:
    """A populated-existing query must not discard pending criterion head changes."""
    submitted = owner.submit_evidence(rig.db, command=submission(rig))
    assert submitted.submission_id is not None and rig.enrollment is not None
    owner.decide(rig.db, command=decision(rig, submitted.submission_id))
    rig.db.autoflush = False
    published = owner.publish_criteria(
        rig.db,
        command=PublishCompletionCriteria(
            context=context(rig.staff, WRITE),
            access=access(rig.staff),
            enrollment_id=rig.enrollment,
            expected_generation=1,
            criteria=tuple(
                IssuedCompletionCriterion(
                    id=identity,
                    order=index,
                    statement="Changed required repair",
                    verification_method="Measure again",
                )
                for index, identity in enumerate(rig.criteria)
            ),
        ),
    )
    assert published.criteria_generation == 2
    head = rig.db.get(CompletionCriteriaHead, rig.enrollment, populate_existing=True)
    assert head is not None and head.generation == 2
    assert head.revision_id == published.criteria_revision_id
    rig.db.rollback()
    current = owner.current(
        rig.db,
        query=CompletionReviewQuery(
            access=access(rig.staff), enrollment_id=rig.enrollment
        ),
    )
    assert current.state == ReviewState.stale
