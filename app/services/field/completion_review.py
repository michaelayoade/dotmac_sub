"""Issued-work observations and staff review; no operational or financial consequences."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Callable
from dataclasses import asdict
from datetime import UTC, datetime
from typing import TypeVar
from uuid import UUID

from sqlalchemy import and_, or_, select, text, true
from sqlalchemy.orm import Session

from app.models.dispatch import TechnicianProfile
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
from app.models.project_engineering_plan import EngineeringPlanCriterion
from app.models.project_work_order_issuance import ProjectWorkOrderIssuance
from app.models.system_user import SystemUser
from app.models.work_order import WorkOrder
from app.services.action_readiness import (
    ActionableBlocker,
    ActionReadiness,
    BlockerEvidence,
    ReadinessState,
)
from app.services.audit_adapter import AuditActor, AuditRecord, audit_adapter
from app.services.domain_errors import DomainError
from app.services.events import EventType, emit_event
from app.services.field.completion_review_contracts import (
    CONCERN,
    OWNER,
    READ,
    WRITE,
    CompletionBasisParticipant,
    CompletionEvidenceContent,
    CompletionEvidenceDisplay,
    CompletionEvidenceQuery,
    CompletionPolicyPurpose,
    CompletionPolicyQuery,
    CompletionPolicyScope,
    CompletionPolicyScopeKind,
    CompletionPolicySnapshot,
    CompletionProvenance,
    CompletionReviewHistory,
    CompletionReviewOutcome,
    CompletionReviewQuery,
    CompletionReviewRecord,
    CompletionSubmissionRecord,
    CompletionWorkspace,
    CompletionWorkspacePurpose,
    CompletionWorkspaceQuery,
    CriterionCoverage,
    CriterionObservation,
    CriterionResult,
    CriterionVerdict,
    CurrentReplacementEvidence,
    CurrentReplacementEvidenceQuery,
    DecideCompletionReview,
    EngineeringReplacementScope,
    EnrollCompletionReview,
    FailedScopeReplacementOutcome,
    FailedScopeReplacementRecord,
    FailedScopeReplacementWorkspace,
    FailedScopeReplacementWorkspaceQuery,
    FieldSubmissionPreview,
    FieldSubmissionPreviewQuery,
    IssuedCompletionCriterion,
    LinkedTicketEnrollmentQuery,
    LinkedTicketEnrollments,
    NativeTicketReplacementScope,
    OutageInfrastructureReplacementScope,
    PublishCompletionCriteria,
    PublishCompletionReviewPolicy,
    RejectedSubmissionFailure,
    ReplacementEnrollmentSnapshot,
    ReplacementScope,
    ReplacementState,
    ReplacementWorkspacePurpose,
    ReviewFailedScopeReplacement,
    ReviewState,
    RevokeCompletionReview,
    RevokeFailedScopeReplacement,
    ScopedCompletionBasis,
    ScopedCompletionBasisPreviewQuery,
    ScopedCompletionBasisQuery,
    SubmitCompletionEvidence,
    SupportTicketCompletionPreviewQuery,
    TicketCompletionBasis,
    TicketCompletionBasisEntry,
    TicketCompletionBasisQuery,
    UnableToCompleteFailure,
)
from app.services.field.work_order_access import (
    FieldActor,
    FieldWorkOrderScope,
    ResolveFieldActor,
    require_work_order,
    resolve_field_actor,
)
from app.services.file_storage import (
    CompletionFileRetentionRegistration,
    FileEvidenceLock,
    RetainedEvidenceStreamQuery,
    lock_files_for_evidence,
    stage_completion_retention,
    stream_retained_evidence,
)
from app.services.owner_commands import (
    OwnerCommandDefinition,
    execute_owner_command,
    owner_command_active,
)
from app.services.permission_admission import PermissionAdmission
from app.services.projects import (
    engineering_plan_revocation_scope,
)
from app.services.staff_permission_admission import (
    AuthorizedStaffPerson,
    revalidate_staff_admission,
)
from app.services.work_order_assignment_contracts import (
    CompletionEnrollmentLinkagePreviewQuery,
    CompletionEnrollmentLinkageQuery,
    CurrentWorkOrderAssignmentQuery,
    WorkOrderLinkageFence,
)
from app.services.work_order_commands import (
    lock_work_order_linkage,
    preview_completion_enrollment_linkage,
    stage_completion_enrollment_linkage,
    work_order_commands,
)
from app.services.work_order_errors import WorkOrderCommandError

ReviewCommand = (
    PublishCompletionReviewPolicy
    | SubmitCompletionEvidence
    | DecideCompletionReview
    | RevokeCompletionReview
    | EnrollCompletionReview
    | PublishCompletionCriteria
)


ReplacementCommand = ReviewFailedScopeReplacement | RevokeFailedScopeReplacement
REPLACEMENT_CONCERN = "reviewed failed completion scope replacement"


def _error(code: str) -> DomainError:
    return DomainError(
        code=f"{OWNER}.{code}", message=code.replace("_", " "), retryable=False
    )


def _fingerprint(command: ReviewCommand | ReplacementCommand) -> str:
    values = asdict(command)
    values.pop("access", None)
    values["action"] = type(command).__name__
    values["context"] = {
        "actor": command.context.actor,
        "scope": command.context.scope,
        "reason": command.context.reason,
    }
    return hashlib.sha256(
        json.dumps(values, sort_keys=True, separators=(",", ":"), default=str).encode()
    ).hexdigest()


def _identity(
    db: Session, command: ReviewCommand | ReplacementCommand
) -> tuple[str, str]:
    context = command.context
    key = context.idempotency_key
    if not key or not key.strip() or len(key) > 120 or not context.reason.strip():
        raise _error("invalid_request")
    if db.get_bind().dialect.name == "postgresql":
        db.execute(
            text("SELECT pg_advisory_xact_lock(:key)"),
            {
                "key": int.from_bytes(
                    hashlib.sha256((OWNER + ":review:" + key).encode()).digest()[:8],
                    "big",
                    signed=True,
                )
            },
        )
    return key, _fingerprint(command)


def _staff(
    db: Session,
    command: PublishCompletionReviewPolicy
    | DecideCompletionReview
    | RevokeCompletionReview
    | EnrollCompletionReview
    | PublishCompletionCriteria
    | ReplacementCommand,
) -> AuthorizedStaffPerson:
    actor = revalidate_staff_admission(
        db, admission=command.access, permission=WRITE, write=True
    )
    if (
        command.context.actor != str(actor.system_user_id)
        or command.context.scope != WRITE
    ):
        raise _error("denied")
    return actor


def _receipt_outcome(
    row: CompletionReviewReceipt, *, replayed: bool
) -> CompletionReviewOutcome:
    return CompletionReviewOutcome(
        enrollment_id=row.enrollment_id,
        policy_revision_id=row.policy_revision_id,
        submission_id=row.submission_id,
        decision_id=row.decision_id,
        generation=row.generation,
        state=ReviewState(row.state),
        replayed=replayed,
        criteria_revision_id=row.criteria_revision_id,
        criteria_generation=row.criteria_generation,
        completed_event_id=row.completed_event_id,
    )


def _replay(
    db: Session, *, key: str, fingerprint: str
) -> CompletionReviewOutcome | None:
    if db.get(CompletionReplacementReceipt, key, populate_existing=True) is not None:
        raise _error("idempotency_conflict")
    row = db.get(CompletionReviewReceipt, key, populate_existing=True)
    if row is None:
        return None
    if row.fingerprint != fingerprint:
        raise _error("idempotency_conflict")
    return _receipt_outcome(row, replayed=True)


def _finish(
    db: Session,
    *,
    command: ReviewCommand,
    key: str,
    fingerprint: str,
    outcome: CompletionReviewOutcome,
) -> CompletionReviewOutcome:
    db.add(
        CompletionReviewReceipt(
            idempotency_key=key,
            fingerprint=fingerprint,
            enrollment_id=outcome.enrollment_id,
            criteria_revision_id=outcome.criteria_revision_id,
            criteria_generation=outcome.criteria_generation,
            completed_event_id=outcome.completed_event_id,
            policy_revision_id=outcome.policy_revision_id,
            submission_id=outcome.submission_id,
            decision_id=outcome.decision_id,
            generation=outcome.generation,
            state=outcome.state.value,
        )
    )
    details: dict[str, object] = {
        "enrollment_id": str(outcome.enrollment_id) if outcome.enrollment_id else None,
        "policy_revision_id": str(outcome.policy_revision_id)
        if outcome.policy_revision_id
        else None,
        "submission_id": str(outcome.submission_id) if outcome.submission_id else None,
        "decision_id": str(outcome.decision_id) if outcome.decision_id else None,
        "generation": outcome.generation,
        "state": outcome.state.value,
        "action": type(command).__name__,
        "command_id": str(command.context.command_id),
        "correlation_id": str(command.context.correlation_id),
        "reason": command.context.reason,
    }
    audit_adapter.stage(
        db,
        AuditRecord(
            action="field.completion_review_recorded",
            entity_type="completion_review",
            entity_id=str(outcome.enrollment_id or outcome.policy_revision_id),
            actor=AuditActor.user(command.context.actor),
            details=details,
            metadata=details,
        ),
    )
    emit_event(
        db,
        EventType.field_completion_review_recorded,
        details,
        actor=command.context.actor,
    )
    db.flush()
    return outcome


_Outcome = TypeVar("_Outcome")


def _run_owner(
    db: Session,
    *,
    command: ReviewCommand | ReplacementCommand,
    concern: str,
    operation: Callable[[], _Outcome],
) -> _Outcome:
    return execute_owner_command(
        db,
        definition=OwnerCommandDefinition(
            owner=OWNER, concern=concern, name=type(command).__name__
        ),
        context=command.context,
        operation=operation,
    )


def _execute(db: Session, *, command: ReviewCommand) -> CompletionReviewOutcome:
    return _run_owner(
        db,
        command=command,
        concern=CONCERN,
        operation=lambda: _stage(db, command=command),
    )


def publish_policy(
    db: Session, *, command: PublishCompletionReviewPolicy
) -> CompletionReviewOutcome:
    return _execute(db, command=command)


def submit_evidence(
    db: Session, *, command: SubmitCompletionEvidence
) -> CompletionReviewOutcome:
    return _execute(db, command=command)


def decide(db: Session, *, command: DecideCompletionReview) -> CompletionReviewOutcome:
    return _execute(db, command=command)


def revoke(db: Session, *, command: RevokeCompletionReview) -> CompletionReviewOutcome:
    return _execute(db, command=command)


def _enrollment(db: Session, enrollment_id: UUID) -> CompletionEnrollment:
    row = db.get(CompletionEnrollment, enrollment_id)
    if row is None:
        raise _error("not_found")
    return row


def _policy(
    db: Session, *, enrollment: CompletionEnrollment, revision_id: UUID
) -> CompletionReviewPolicy:
    head = _policy_head(db, enrollment.policy_scope_type, enrollment.policy_scope_id)
    policy = db.get(CompletionReviewPolicy, revision_id)
    if (
        head is None
        or head.revision_id != revision_id
        or policy is None
        or (policy.scope_type, policy.scope_id)
        != (enrollment.policy_scope_type, enrollment.policy_scope_id)
    ):
        raise _error("stale_policy")
    return policy


def _head(
    db: Session, *, enrollment_id: UUID, generation: int
) -> CompletionReviewHead | None:
    head = db.scalar(
        select(CompletionReviewHead)
        .where(CompletionReviewHead.enrollment_id == enrollment_id)
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    if (head.generation if head else 0) != generation:
        raise _error("stale_state")
    return head


def _criteria(db: Session, revision_id: UUID) -> frozenset[UUID]:
    return frozenset(
        db.scalars(
            select(CompletionCriterion.id).where(
                CompletionCriterion.revision_id == revision_id
            )
        ).all()
    )


def _exact_criteria(expected: frozenset[UUID], actual: tuple[UUID, ...]) -> None:
    if not expected or len(actual) != len(set(actual)) or frozenset(actual) != expected:
        raise _error("criterion_mismatch")


def _field_admit(
    db: Session,
    *,
    command: SubmitCompletionEvidence,
    enrollment: CompletionEnrollment,
    lock: bool,
) -> FieldActor:
    actor = resolve_field_actor(db, ResolveFieldActor(command.requester_system_user_id))
    if (
        command.context.actor != str(actor.system_user_id)
        or command.context.scope != "field:completion:submit"
    ):
        raise _error("denied")
    row = require_work_order(
        db, FieldWorkOrderScope(actor, enrollment.public_id, lock=lock)
    )
    assignment = work_order_commands.current_assignment(
        db, query=CurrentWorkOrderAssignmentQuery(row.id)
    )
    if (
        not _scope_matches(row, enrollment)
        or assignment is None
        or assignment.generation is None
        or assignment.generation != command.assignment_generation
    ):
        raise _error("assignment_generation_mismatch")
    return actor


def _stage(db: Session, *, command: ReviewCommand) -> CompletionReviewOutcome:
    key, fingerprint = _identity(db, command)
    if isinstance(command, SubmitCompletionEvidence):
        return _submit(db, command=command, key=key, fingerprint=fingerprint)
    actor = _staff(db, command)
    if (
        isinstance(
            command,
            (DecideCompletionReview, RevokeCompletionReview, PublishCompletionCriteria),
        )
        and command.expected_work_order_public_id is not None
    ):
        if (
            _enrollment(db, command.enrollment_id).public_id
            != command.expected_work_order_public_id
        ):
            raise _error("scope_mismatch")
    replay = _replay(db, key=key, fingerprint=fingerprint)
    if replay is not None:
        if _staff(db, command) != actor:
            raise _error("denied")
        return _decorate(db, replay)
    if isinstance(command, PublishCompletionReviewPolicy):
        outcome = _publish_policy(db, command=command, actor=actor)
    elif isinstance(command, EnrollCompletionReview):
        outcome = _enroll(db, command=command, actor=actor)
    elif isinstance(command, PublishCompletionCriteria):
        enrollment = _enrollment(db, command.enrollment_id)
        _origin_locks(db, enrollment)
        _work_order(db, enrollment)
        if enrollment.provenance != CompletionProvenance.native_work_order:
            raise _error("provenance_mismatch")
        criteria_head = _criteria_head(db, enrollment)
        if criteria_head.generation != command.expected_generation:
            raise _error("stale_state")
        revision = _write_criteria(
            db,
            enrollment=enrollment,
            criteria=command.criteria,
            actor=actor,
            reason=command.context.reason,
            revision=criteria_head.generation + 1,
        )
        criteria_head.generation += 1
        criteria_head.revision_id = revision.id
        outcome = _empty(enrollment.id)
    else:
        outcome = _review(db, command=command, actor=actor)
    if _staff(db, command) != actor:
        raise _error("denied")
    db.flush()
    outcome = _decorate(db, outcome)
    return _finish(
        db, command=command, key=key, fingerprint=fingerprint, outcome=outcome
    )


def _submit(
    db: Session, *, command: SubmitCompletionEvidence, key: str, fingerprint: str
) -> CompletionReviewOutcome:
    enrollment = _enrollment(db, command.enrollment_id)
    if (
        command.expected_work_order_public_id is not None
        and command.expected_work_order_public_id != enrollment.public_id
    ):
        raise _error("scope_mismatch")
    actor = resolve_field_actor(db, ResolveFieldActor(command.requester_system_user_id))
    # Principal before origin and visit
    # vendor membership locks remain after visit,
    # matching canonical dispatch and field admission.
    db.scalar(
        select(SystemUser)
        .where(SystemUser.id == actor.system_user_id)
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    if actor.technician_id is not None:
        db.scalar(
            select(TechnicianProfile)
            .where(TechnicianProfile.id == actor.technician_id)
            .with_for_update()
            .execution_options(populate_existing=True)
        )
    _origin_locks(db, enrollment)
    actor = _field_admit(db, command=command, enrollment=enrollment, lock=True)
    replay = _replay(db, key=key, fingerprint=fingerprint)
    if replay is not None:
        return _decorate(db, replay)
    criteria_head = _criteria_head(db, enrollment)
    if command.criteria_revision_id != criteria_head.revision_id:
        raise _error("stale_criteria")
    _policy(db, enrollment=enrollment, revision_id=command.policy_revision_id)
    work_order = _work_order(db, enrollment)
    event = _completed(db, work_order)
    if (
        event is None
        or event.id != command.completed_event_id
        or event.assignment_generation != command.assignment_generation
        or event.system_user_id != actor.system_user_id
        or event.author_vendor_user_id != actor.vendor_user_id
        or event.author_technician_id != actor.technician_id
    ):
        raise _error("completion_required")
    head = _head(
        db, enrollment_id=enrollment.id, generation=command.expected_generation
    )
    _exact_criteria(
        _criteria(db, _criteria_head(db, enrollment).revision_id),
        tuple(value.criterion_id for value in command.observations),
    )
    if any(
        not value.observation.strip()
        or len(value.observation) > 8000
        or len(value.attachment_ids) != len(set(value.attachment_ids))
        for value in command.observations
    ):
        raise _error("invalid_request")
    ids = frozenset(
        value
        for observation in command.observations
        for value in observation.attachment_ids
    )
    attachments = {
        row.id: row
        for row in db.scalars(
            select(FieldAttachment).where(FieldAttachment.id.in_(ids))
        ).all()
    }
    if frozenset(attachments) != ids:
        raise _error("evidence_unavailable")
    # Submission and deletion share the StoredFile lock before attachment locks.
    files = {
        row.id: row
        for row in lock_files_for_evidence(
            db,
            query=FileEvidenceLock(
                tuple(sorted({row.stored_file_id for row in attachments.values()}))
            ),
        )
    }
    attachments = {
        row.id: row
        for row in db.scalars(
            select(FieldAttachment)
            .where(FieldAttachment.id.in_(ids))
            .order_by(FieldAttachment.id)
            .with_for_update()
            .execution_options(populate_existing=True)
        ).all()
    }
    for attachment in attachments.values():
        stored = files.get(attachment.stored_file_id)
        if (
            not attachment.is_active
            or attachment.work_order_mirror_id != enrollment.work_order_id
            or stored is None
            or stored.is_deleted
            or not stored.checksum
            or len(stored.checksum) != 64
            or stored.entity_type != "field_attachment"
            or stored.entity_id != enrollment.public_id
        ):
            raise _error("evidence_unavailable")
    # Revalidate exact selected member/generation after all lock waits.
    if _field_admit(db, command=command, enrollment=enrollment, lock=True) != actor:
        raise _error("denied")
    user = db.get(SystemUser, actor.system_user_id, populate_existing=True)
    row = CompletionSubmission(
        enrollment_id=enrollment.id,
        policy_revision_id=command.policy_revision_id,
        criteria_revision_id=command.criteria_revision_id,
        completed_event_id=command.completed_event_id,
        assignment_generation=command.assignment_generation,
        actor_user_id=actor.system_user_id,
        actor_person_id=user.person_party_id if user else None,
        actor_vendor_user_id=actor.vendor_user_id,
        reason=command.context.reason,
    )
    db.add(row)
    db.flush()
    for observation in command.observations:
        db.add(
            CompletionObservation(
                submission_id=row.id,
                criteria_revision_id=command.criteria_revision_id,
                criterion_id=observation.criterion_id,
                observation=observation.observation,
            )
        )
    db.flush()
    for stored_file_id in sorted(
        {value.stored_file_id for value in attachments.values()}
    ):
        stage_completion_retention(
            db, command=CompletionFileRetentionRegistration(stored_file_id, row.id)
        )
    for observation in command.observations:
        for attachment_id in observation.attachment_ids:
            attachment = attachments[attachment_id]
            stored = files[attachment.stored_file_id]
            db.add(
                CompletionEvidenceFile(
                    submission_id=row.id,
                    criterion_id=observation.criterion_id,
                    attachment_id=attachment_id,
                    stored_file_id=stored.id,
                    checksum=stored.checksum,
                )
            )
    if head is None:
        head = CompletionReviewHead(
            enrollment_id=enrollment.id, generation=1, submission_id=row.id
        )
        db.add(head)
    else:
        head.generation += 1
        head.submission_id = row.id
        head.decision_id = None
    outcome = CompletionReviewOutcome(
        enrollment_id=enrollment.id,
        policy_revision_id=command.policy_revision_id,
        submission_id=row.id,
        decision_id=None,
        generation=head.generation,
        state=ReviewState.submitted,
    )
    return _finish(
        db,
        command=command,
        key=key,
        fingerprint=fingerprint,
        outcome=_decorate(db, outcome),
    )


def _review(
    db: Session,
    *,
    command: DecideCompletionReview | RevokeCompletionReview,
    actor: AuthorizedStaffPerson,
) -> CompletionReviewOutcome:
    enrollment = _enrollment(db, command.enrollment_id)
    if isinstance(command, RevokeCompletionReview):
        _origin_locks(db, enrollment)
    else:
        _origin_locks(db, enrollment)
    if isinstance(command, DecideCompletionReview):
        _engineering_current(
            db, enrollment=enrollment, access=command.access, purpose="stage"
        )
    work_order = db.scalar(
        select(WorkOrder)
        .where(WorkOrder.id == enrollment.work_order_id)
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    if work_order is None or not _scope_matches(work_order, enrollment):
        raise _error("scope_mismatch")
    # Policy is always before head, matching submission.
    if isinstance(command, DecideCompletionReview):
        policy = _policy(
            db, enrollment=enrollment, revision_id=command.policy_revision_id
        )
    head = _head(
        db, enrollment_id=enrollment.id, generation=command.expected_generation
    )
    if head is None:
        raise _error("stale_state")
    submission = db.get(CompletionSubmission, head.submission_id)
    if submission is None:
        raise _error("stale_state")
    if isinstance(command, DecideCompletionReview):
        if not _submission_current(db, enrollment, submission, work_order):
            raise _error("stale_state")
        if (
            command.submission_id != head.submission_id
            or command.policy_revision_id != submission.policy_revision_id
            or head.decision_id is not None
        ):
            raise _error("stale_state")
        if policy.independent_reviewer_required and (
            submission.actor_person_id is None
            or actor.system_user_id == submission.actor_user_id
            or actor.person_id == submission.actor_person_id
        ):
            raise _error("independent_reviewer_required")
        _exact_criteria(
            _criteria(db, _criteria_head(db, enrollment).revision_id),
            tuple(value.criterion_id for value in command.results),
        )
        if any(
            not value.reason.strip() or len(value.reason) > 4000
            for value in command.results
        ):
            raise _error("invalid_request")
        state = (
            ReviewState.accepted
            if all(
                value.verdict == CriterionVerdict.passed for value in command.results
            )
            else ReviewState.rejected
        )
        row = CompletionReviewDecision(
            enrollment_id=enrollment.id,
            submission_id=submission.id,
            policy_revision_id=policy.id,
            state=state.value,
            actor_user_id=actor.system_user_id,
            actor_person_id=actor.person_id,
            reason=command.context.reason,
        )
    else:
        previous = db.get(CompletionReviewDecision, command.decision_id)
        if (
            previous is None
            or previous.id != head.decision_id
            or previous.enrollment_id != enrollment.id
            or previous.state == ReviewState.revoked.value
        ):
            raise _error("stale_state")
        state = ReviewState.revoked
        row = CompletionReviewDecision(
            enrollment_id=enrollment.id,
            submission_id=submission.id,
            policy_revision_id=previous.policy_revision_id,
            state=state.value,
            revokes_id=previous.id,
            actor_user_id=actor.system_user_id,
            actor_person_id=actor.person_id,
            reason=command.context.reason,
        )
    if _staff(db, command) != actor:
        raise _error("denied")
    db.add(row)
    db.flush()
    if isinstance(command, DecideCompletionReview):
        for value in command.results:
            db.add(
                CompletionCriterionResult(
                    decision_id=row.id,
                    submission_id=submission.id,
                    criterion_id=value.criterion_id,
                    verdict=value.verdict.value,
                    reason=value.reason,
                )
            )
    head.generation += 1
    head.decision_id = row.id
    return CompletionReviewOutcome(
        enrollment_id=enrollment.id,
        policy_revision_id=row.policy_revision_id,
        submission_id=submission.id,
        decision_id=row.id,
        generation=head.generation,
        state=state,
    )


def history(db: Session, *, query: CompletionReviewQuery) -> CompletionReviewHistory:
    revalidate_staff_admission(db, admission=query.access, permission=READ, write=False)
    enrollment = _enrollment(db, query.enrollment_id)
    return _history(db, enrollment=enrollment)


def _history(
    db: Session,
    *,
    enrollment: CompletionEnrollment,
    submission_id: UUID | None = None,
) -> CompletionReviewHistory:
    decisions = db.scalars(
        select(CompletionReviewDecision)
        .where(
            CompletionReviewDecision.enrollment_id == enrollment.id,
            true()
            if submission_id is None
            else CompletionReviewDecision.submission_id == submission_id,
        )
        .order_by(CompletionReviewDecision.decided_at, CompletionReviewDecision.id)
    ).all()
    return CompletionReviewHistory(
        enrollment_id=enrollment.id,
        submissions=tuple(
            CompletionSubmissionRecord(
                id=row.id,
                policy_revision_id=row.policy_revision_id,
                criteria_revision_id=row.criteria_revision_id,
                completed_event_id=row.completed_event_id,
                assignment_generation=row.assignment_generation,
                actor_system_user_id=row.actor_user_id,
                actor_vendor_user_id=row.actor_vendor_user_id,
                submitted_at=row.submitted_at,
                criteria=tuple(
                    IssuedCompletionCriterion(
                        id=c.id,
                        order=c.order,
                        statement=c.statement,
                        verification_method=c.verification_method,
                    )
                    for c in db.scalars(
                        select(CompletionCriterion)
                        .where(
                            CompletionCriterion.revision_id == row.criteria_revision_id
                        )
                        .order_by(CompletionCriterion.order)
                    )
                ),
                observations=tuple(
                    CriterionObservation(
                        criterion_id=observation.criterion_id,
                        observation=observation.observation,
                        attachment_ids=tuple(
                            db.scalars(
                                select(CompletionEvidenceFile.attachment_id)
                                .where(
                                    CompletionEvidenceFile.submission_id == row.id,
                                    CompletionEvidenceFile.criterion_id
                                    == observation.criterion_id,
                                )
                                .order_by(CompletionEvidenceFile.attachment_id)
                            ).all()
                        ),
                    )
                    for observation in db.scalars(
                        select(CompletionObservation)
                        .where(CompletionObservation.submission_id == row.id)
                        .order_by(CompletionObservation.criterion_id)
                    ).all()
                ),
            )
            for row in db.scalars(
                select(CompletionSubmission)
                .where(
                    CompletionSubmission.enrollment_id == enrollment.id,
                    true()
                    if submission_id is None
                    else CompletionSubmission.id == submission_id,
                )
                .order_by(CompletionSubmission.submitted_at, CompletionSubmission.id)
            ).all()
        ),
        decisions=tuple(
            CompletionReviewRecord(
                decided_at=row.decided_at,
                id=row.id,
                submission_id=row.submission_id,
                state=ReviewState(row.state),
                reviewer_system_user_id=row.actor_user_id,
                reason=row.reason,
                policy_revision_id=row.policy_revision_id,
                results=tuple(
                    CriterionResult(
                        criterion_id=result.criterion_id,
                        verdict=CriterionVerdict(result.verdict),
                        reason=result.reason,
                    )
                    for result in db.scalars(
                        select(CompletionCriterionResult)
                        .where(CompletionCriterionResult.decision_id == row.id)
                        .order_by(CompletionCriterionResult.criterion_id)
                    ).all()
                ),
            )
            for row in decisions
        ),
    )


def current(db: Session, *, query: CompletionReviewQuery) -> CompletionReviewOutcome:
    return _decorate(db, _current(db, query=query, permission=READ)[0])


def _current(
    db: Session,
    *,
    query: CompletionReviewQuery,
    permission: str,
    check_engineering: bool = False,
    skip_engineering: bool = False,
) -> tuple[CompletionReviewOutcome, bool]:
    reader = revalidate_staff_admission(
        db, admission=query.access, permission=permission, write=permission == WRITE
    )
    enrollment = _enrollment(db, query.enrollment_id)
    # Project is the shared serialization point for every canonical head/policy writer.
    # Lock before reading any mutable completion state, including archived origins.
    _origin_locks(db, enrollment)
    observed = db.get(CompletionReviewHead, enrollment.id, populate_existing=True)
    decision = (
        db.get(CompletionReviewDecision, observed.decision_id)
        if observed and observed.decision_id
        else None
    )
    engineering_stale = False
    if not skip_engineering and (
        check_engineering or (decision and decision.state == ReviewState.accepted.value)
    ):
        try:
            _engineering_current(
                db,
                enrollment=enrollment,
                access=query.access,
                purpose="write" if permission == WRITE else "read",
            )
        except DomainError as exc:
            if exc.code not in {
                "operations.project_engineering_plans.unapproved",
                "operations.project_engineering_plans.stale_state",
                "operations.project_lifecycle.relationship_conflict",
            }:
                raise
            engineering_stale = True
    work_order = db.scalar(
        select(WorkOrder)
        .where(WorkOrder.id == enrollment.work_order_id)
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    criteria_head = _criteria_head(db, enrollment)
    policy = _policy_head(db, enrollment.policy_scope_type, enrollment.policy_scope_id)
    head = db.scalar(
        select(CompletionReviewHead)
        .where(CompletionReviewHead.enrollment_id == enrollment.id)
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    if (
        revalidate_staff_admission(
            db, admission=query.access, permission=permission, write=permission == WRITE
        )
        != reader
    ):
        raise _error("denied")
    if head is None:
        return (
            CompletionReviewOutcome(
                enrollment_id=enrollment.id,
                policy_revision_id=None,
                submission_id=None,
                decision_id=None,
                generation=0,
                state=ReviewState.legacy_not_reviewed,
            ),
            engineering_stale,
        )
    submission = db.get(CompletionSubmission, head.submission_id)
    decision = (
        db.get(CompletionReviewDecision, head.decision_id) if head.decision_id else None
    )
    state = ReviewState(decision.state) if decision else ReviewState.submitted
    if state == ReviewState.accepted and (
        engineering_stale
        or submission is None
        or not _submission_current(db, enrollment, submission, work_order)
        or work_order is None
        or work_order.project_id != enrollment.project_id
        or work_order.project_task_id != enrollment.task_id
        or policy is None
        or decision is None
        or policy.revision_id != decision.policy_revision_id
    ):
        state = ReviewState.stale
    return (
        CompletionReviewOutcome(
            enrollment_id=enrollment.id,
            policy_revision_id=submission.policy_revision_id if submission else None,
            submission_id=head.submission_id,
            decision_id=head.decision_id,
            generation=head.generation,
            state=state,
        ),
        engineering_stale,
    )


def _readiness(action: str, subject: str, blocker: str | None) -> ActionReadiness:
    messages = {
        "origin_ticket_terminal": "The origin ticket must be active and nonterminal before enrollment.",
        "enrollment_required": "Staff must explicitly enroll this work order for completion review.",
        "completion_required": "Record the assigned field completion event before submitting observations.",
        "criteria_required": "Staff must publish completion criteria first.",
        "already_enrolled": "This work order already has a completion enrollment.",
        "stale_state": "The current assignment, completion event, scope or review criteria changed.",
        "stale_criteria": "Submit observations against the current completion criteria.",
        "write_preview_required": "Open the protected action preview to check availability.",
        "replacement_conflict": "This failed scope already has a reviewed replacement. Revoke that decision before selecting another visit.",
        "replacement_required": "There is no active replacement decision to revoke.",
        "replacement_candidate_required": "No eligible replacement visit is available. Enroll and assign a separate visit for this same ticket and customer.",
        "failure_basis_stale": "This scope has no current failed visit or rejected completion submission to replace.",
        "engineering_replacement_proof_required": "Engineering-issued scopes need an approved scope-equivalence review before replacement is available.",
        "replacement_stale": "Replacement review evidence changed. Review the current failed scope again.",
        "replacement_cycle": "A replacement chain cannot return to an earlier visit.",
        "issuance_required": "An issued engineering work order is required.",
        "policy_required": "Staff must configure a completion review policy first.",
        "assignment_required": "A current assignment is required.",
        "scope_mismatch": "This work order no longer matches its issued project and task.",
        "submission_required": "Submit completion observations before staff review.",
        "decision_exists": "The current submission already has a decision.",
        "decision_required": "A current decision is required for withdrawal.",
        "already_revoked": "The current decision has already been withdrawn.",
        "stale_policy": "Submit observations against the current review policy.",
        "engineering_stale": "The issued engineering approval must be current before review.",
        "independent_reviewer_required": "This policy requires distinct, authoritative submitter and reviewer Persons.",
    }
    message = messages.get(blocker or "", blocker or "")
    return ActionReadiness(
        action_key=f"field_completion.{action}",
        subject_type="completion_review",
        subject_id=subject,
        owner=OWNER,
        state=ReadinessState.blocked if blocker else ReadinessState.ready,
        evaluated_at=datetime.now(UTC),
        blockers=(
            ActionableBlocker(
                code=f"{OWNER}.{blocker}",
                owner={
                    "issuance_required": "operations.project_work_order_handoff",
                    "assignment_required": "operations.work_order_commands",
                    "scope_mismatch": "operations.work_order_commands",
                    "engineering_stale": "operations.project_engineering_plans",
                }.get(blocker or "", OWNER),
                customer_message=message,
                staff_detail=message,
                evidence=BlockerEvidence(summary=message),
            ),
        )
        if blocker
        else (),
    )


def enroll(db: Session, *, command: EnrollCompletionReview) -> CompletionReviewOutcome:
    return _execute(db, command=command)


def publish_criteria(
    db: Session, *, command: PublishCompletionCriteria
) -> CompletionReviewOutcome:
    return _execute(db, command=command)


def _empty(enrollment_id: UUID | None = None) -> CompletionReviewOutcome:
    return CompletionReviewOutcome(
        enrollment_id=enrollment_id,
        policy_revision_id=None,
        submission_id=None,
        decision_id=None,
        generation=0,
        state=ReviewState.legacy_not_reviewed,
    )


def _origin_locks(db: Session, enrollment: CompletionEnrollment) -> None:
    if enrollment.project_id is not None:
        engineering_plan_revocation_scope(
            db, project_id=enrollment.project_id, task_id=enrollment.task_id
        )


def _scope_matches(row: WorkOrder, enrollment: CompletionEnrollment) -> bool:
    return (
        row.id,
        row.public_id,
        row.project_id,
        row.project_task_id,
        row.subscriber_id,
        row.origin_ticket_id,
    ) == (
        enrollment.work_order_id,
        enrollment.public_id,
        enrollment.project_id,
        enrollment.task_id,
        enrollment.subscriber_id,
        enrollment.origin_ticket_id,
    )


def _work_order(db: Session, enrollment: CompletionEnrollment) -> WorkOrder:
    row = db.scalar(
        select(WorkOrder)
        .where(WorkOrder.id == enrollment.work_order_id)
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    if row is None or not _scope_matches(row, enrollment):
        raise _error("scope_mismatch")
    return row


def _criteria_head(
    db: Session, enrollment: CompletionEnrollment
) -> CompletionCriteriaHead:
    row = db.scalar(
        select(CompletionCriteriaHead)
        .where(CompletionCriteriaHead.enrollment_id == enrollment.id)
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    if row is None:
        raise _error("criteria_required")
    return row


def _policy_head(
    db: Session, scope_type: str, scope_id: UUID
) -> CompletionReviewPolicyHead | None:
    return db.scalar(
        select(CompletionReviewPolicyHead)
        .where(
            CompletionReviewPolicyHead.scope_type == scope_type,
            CompletionReviewPolicyHead.scope_id == scope_id,
        )
        .with_for_update()
        .execution_options(populate_existing=True)
    )


def _scope(
    db: Session, scope: CompletionPolicyScope, *, lock: bool
) -> tuple[str, UUID, UUID | None, UUID | None]:
    if scope.kind == CompletionPolicyScopeKind.project:
        if scope.project_id is None:
            raise _error("invalid_request")
        if lock:
            engineering_plan_revocation_scope(
                db, project_id=scope.project_id, task_id=None
            )
        return scope.kind.value, scope.project_id, scope.project_id, None
    row = db.scalar(
        select(WorkOrder).where(WorkOrder.public_id == scope.work_order_public_id)
    )
    if row is None:
        raise _error("not_found")
    if lock:
        lock_work_order_linkage(
            db,
            query=WorkOrderLinkageFence(
                ticket_id=row.origin_ticket_id, work_order_id=row.id
            ),
        )
        if row.project_id is not None:
            engineering_plan_revocation_scope(
                db, project_id=row.project_id, task_id=row.project_task_id
            )
        row = db.scalar(
            select(WorkOrder)
            .where(WorkOrder.id == row.id)
            .with_for_update()
            .execution_options(populate_existing=True)
        )
        if row is None or row.public_id != scope.work_order_public_id:
            raise _error("scope_mismatch")
    return scope.kind.value, row.id, None, row.id


def _enrollment_scope(enrollment: CompletionEnrollment) -> CompletionPolicyScope:
    return CompletionPolicyScope(
        kind=CompletionPolicyScopeKind(enrollment.policy_scope_type),
        project_id=enrollment.project_id
        if enrollment.policy_scope_type == "project"
        else None,
        work_order_public_id=enrollment.public_id
        if enrollment.policy_scope_type == "work_order"
        else None,
    )


def _publish_policy(
    db: Session, *, command: PublishCompletionReviewPolicy, actor: AuthorizedStaffPerson
) -> CompletionReviewOutcome:
    kind, identity, project_id, work_order_id = _scope(db, command.scope, lock=True)
    head = _policy_head(db, kind, identity)
    generation = head.generation if head else 0
    if generation != command.expected_generation:
        raise _error("stale_policy")
    row = CompletionReviewPolicy(
        scope_type=kind,
        scope_id=identity,
        project_id=project_id,
        work_order_id=work_order_id,
        revision=generation + 1,
        independent_reviewer_required=command.independent_reviewer_required,
        actor_user_id=actor.system_user_id,
        actor_person_id=actor.person_id,
        reason=command.context.reason,
    )
    db.add(row)
    db.flush()
    if head:
        head.generation += 1
        head.revision_id = row.id
    else:
        db.add(
            CompletionReviewPolicyHead(
                scope_type=kind, scope_id=identity, generation=1, revision_id=row.id
            )
        )
    return CompletionReviewOutcome(
        enrollment_id=None,
        policy_revision_id=row.id,
        submission_id=None,
        decision_id=None,
        generation=generation + 1,
        state=ReviewState.legacy_not_reviewed,
    )


def _write_criteria(
    db: Session,
    *,
    enrollment: CompletionEnrollment,
    criteria: tuple[IssuedCompletionCriterion, ...],
    actor: AuthorizedStaffPerson,
    reason: str,
    revision: int,
    engineering_revision_id: UUID | None = None,
) -> CompletionCriteriaRevision:
    if (
        not criteria
        or len(criteria) > 100
        or len({item.id for item in criteria}) != len(criteria)
        or {item.order for item in criteria} != set(range(len(criteria)))
        or any(
            not item.statement.strip()
            or not item.verification_method.strip()
            or len(item.statement) > 8000
            or len(item.verification_method) > 8000
            for item in criteria
        )
    ):
        raise _error("criterion_mismatch")
    row = CompletionCriteriaRevision(
        enrollment_id=enrollment.id,
        revision=revision,
        engineering_revision_id=engineering_revision_id,
        actor_user_id=actor.system_user_id,
        actor_person_id=actor.person_id,
        reason=reason,
    )
    db.add(row)
    db.flush()
    for item in criteria:
        db.add(
            CompletionCriterion(
                revision_id=row.id,
                id=item.id,
                order=item.order,
                statement=item.statement,
                verification_method=item.verification_method,
            )
        )
    return row


def _enroll(
    db: Session, *, command: EnrollCompletionReview, actor: AuthorizedStaffPerson
) -> CompletionReviewOutcome:
    observed = db.scalar(
        select(WorkOrder).where(WorkOrder.public_id == command.work_order_public_id)
    )
    if observed is None:
        raise _error("not_found")
    observed_links = (
        observed.origin_ticket_id,
        observed.project_id,
        observed.project_task_id,
    )
    lock_work_order_linkage(
        db,
        query=WorkOrderLinkageFence(
            ticket_id=observed.origin_ticket_id, work_order_id=observed.id
        ),
    )
    try:
        stage_completion_enrollment_linkage(
            db,
            query=CompletionEnrollmentLinkageQuery(
                work_order_id=observed.id, origin_ticket_id=observed.origin_ticket_id
            ),
        )
    except WorkOrderCommandError as exc:
        raise _error(exc.code) from exc
    if observed.project_id:
        engineering_plan_revocation_scope(
            db, project_id=observed.project_id, task_id=observed.project_task_id
        )
    row = db.scalar(
        select(WorkOrder)
        .where(WorkOrder.id == observed.id)
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    if (
        row is None
        or (row.origin_ticket_id, row.project_id, row.project_task_id) != observed_links
    ):
        raise _error("scope_mismatch")
    if (
        db.scalar(
            select(CompletionEnrollment.id).where(
                CompletionEnrollment.work_order_id == row.id
            )
        )
        is not None
    ):
        raise _error("already_enrolled")
    actual = db.scalar(
        select(ProjectWorkOrderIssuance).where(
            ProjectWorkOrderIssuance.work_order_id == row.id
        )
    )
    criteria = command.criteria
    engineering_revision_id = None
    if command.provenance == CompletionProvenance.engineering_issuance:
        if (
            actual is None
            or command.engineering_issuance_id != actual.id
            or criteria
            or (actual.project_id, actual.task_id)
            != (row.project_id, row.project_task_id)
        ):
            raise _error("provenance_mismatch")
        engineering_revision_id = actual.revision_id
        criteria = tuple(
            IssuedCompletionCriterion(
                id=value.id,
                order=value.order,
                statement=value.statement,
                verification_method=value.verification_method,
            )
            for value in db.scalars(
                select(EngineeringPlanCriterion)
                .where(EngineeringPlanCriterion.revision_id == actual.revision_id)
                .order_by(EngineeringPlanCriterion.order)
            ).all()
        )
    elif (
        command.provenance != CompletionProvenance.native_work_order
        or actual is not None
        or command.engineering_issuance_id is not None
    ):
        raise _error("provenance_mismatch")
    kind, identity, project_id, scoped_work_order = _scope(
        db, command.policy_scope, lock=False
    )
    if (kind == "project" and project_id != row.project_id) or (
        kind == "work_order" and scoped_work_order != row.id
    ):
        raise _error("scope_mismatch")
    policy = _policy_head(db, kind, identity)
    if policy is None:
        raise _error("policy_required")
    enrolled = CompletionEnrollment(
        work_order_id=row.id,
        public_id=row.public_id,
        provenance=command.provenance.value,
        engineering_issuance_id=actual.id if actual else None,
        project_id=row.project_id,
        task_id=row.project_task_id,
        subscriber_id=row.subscriber_id,
        origin_ticket_id=row.origin_ticket_id,
        policy_scope_type=kind,
        policy_scope_id=identity,
        actor_user_id=actor.system_user_id,
        actor_person_id=actor.person_id,
        reason=command.context.reason,
    )
    db.add(enrolled)
    db.flush()
    if enrolled.project_id is not None:
        from app.services.project_field_completion_contracts import (
            RegisterProjectFieldCompletionRequirement,
        )
        from app.services.projects import stage_register_field_completion_requirement

        stage_register_field_completion_requirement(
            db,
            query=RegisterProjectFieldCompletionRequirement(
                enrollment_id=enrolled.id,
                work_order_id=enrolled.work_order_id,
                project_id=enrolled.project_id,
                task_id=enrolled.task_id,
            ),
        )
    revision = _write_criteria(
        db,
        enrollment=enrolled,
        criteria=criteria,
        actor=actor,
        reason=command.context.reason,
        revision=1,
        engineering_revision_id=engineering_revision_id,
    )
    db.add(
        CompletionCriteriaHead(
            enrollment_id=enrolled.id, revision_id=revision.id, generation=1
        )
    )
    db.flush()
    return _empty(enrolled.id)


def _engineering_current(
    db: Session,
    *,
    enrollment: CompletionEnrollment,
    access: PermissionAdmission | None,
    purpose: str,
) -> None:
    if enrollment.provenance == CompletionProvenance.native_work_order:
        return
    actual = db.get(ProjectWorkOrderIssuance, enrollment.engineering_issuance_id)
    if actual is None or actual.work_order_id != enrollment.work_order_id:
        raise _error("provenance_mismatch")
    from app.services import project_engineering_plans as engineering
    from app.services.project_engineering_plan_contracts import (
        CompletionEngineeringPlanQuery,
        SupportCompletionEngineeringQuery,
    )

    if purpose == "support":
        engineering.stage_current_for_support_completion(
            db,
            query=SupportCompletionEngineeringQuery(
                plan_id=actual.plan_id,
                project_id=actual.project_id,
                task_id=actual.task_id,
                expected_revision_id=actual.revision_id,
                expected_approval_id=actual.approval_id,
                expected_digest=actual.digest,
            ),
        )
    else:
        if access is None:
            raise _error("denied")
        query = CompletionEngineeringPlanQuery(
            access=access,
            plan_id=actual.plan_id,
            project_id=actual.project_id,
            task_id=actual.task_id,
            expected_revision_id=actual.revision_id,
            expected_approval_id=actual.approval_id,
            expected_digest=actual.digest,
        )
        resolver = (
            engineering.stage_current_for_completion
            if purpose == "stage"
            else (
                engineering.resolve_current_for_completion_review
                if purpose == "write"
                else engineering.resolve_current_for_completion
            )
        )
        resolver(db, query=query)


def _completed(db: Session, row: WorkOrder) -> FieldJobEvent | None:
    event = db.scalar(
        select(FieldJobEvent)
        .where(FieldJobEvent.work_order_mirror_id == row.id)
        .order_by(FieldJobEvent.received_at.desc(), FieldJobEvent.id.desc())
        .limit(1)
    )
    if (
        event is None
        or event.event != "complete"
        or event.new_status != "completed"
        or row.status != "completed"
    ):
        return None
    return event


def _submission_current(
    db: Session,
    enrollment: CompletionEnrollment,
    submission: CompletionSubmission,
    row: WorkOrder | None,
) -> bool:
    if row is None or not _scope_matches(row, enrollment):
        return False
    criteria = _criteria_head(db, enrollment)
    policy = _policy_head(db, enrollment.policy_scope_type, enrollment.policy_scope_id)
    assignment = work_order_commands.current_assignment(
        db, query=CurrentWorkOrderAssignmentQuery(row.id)
    )
    event = _completed(db, row)
    return (
        submission.criteria_revision_id == criteria.revision_id
        and policy is not None
        and submission.policy_revision_id == policy.revision_id
        and assignment is not None
        and submission.assignment_generation == assignment.generation
        and event is not None
        and submission.completed_event_id == event.id
        and event.assignment_generation == assignment.generation
        and event.system_user_id == submission.actor_user_id
        and event.author_vendor_user_id == submission.actor_vendor_user_id
    )


def _decorate(db: Session, outcome: CompletionReviewOutcome) -> CompletionReviewOutcome:
    from dataclasses import replace

    if outcome.enrollment_id is None:
        return outcome
    enrollment = _enrollment(db, outcome.enrollment_id)
    head = _criteria_head(db, enrollment)
    row = db.get(WorkOrder, enrollment.work_order_id)
    event = _completed(db, row) if row else None
    submission = (
        db.get(CompletionSubmission, outcome.submission_id)
        if outcome.submission_id
        else None
    )
    if submission is not None:
        if submission.enrollment_id != enrollment.id:
            raise _error("scope_mismatch")
        revision = db.get(CompletionCriteriaRevision, submission.criteria_revision_id)
        if revision is None:
            raise _error("stale_state")
        criteria_revision_id = submission.criteria_revision_id
        criteria_generation = revision.revision
        completed_event_id: UUID | None = submission.completed_event_id
    else:
        criteria_revision_id = outcome.criteria_revision_id or head.revision_id
        criteria_generation = outcome.criteria_generation or head.generation
        completed_event_id = outcome.completed_event_id or (event.id if event else None)
    return replace(
        outcome,
        criteria_revision_id=criteria_revision_id,
        criteria_generation=criteria_generation,
        engineering_issuance_id=enrollment.engineering_issuance_id,
        provenance=CompletionProvenance(enrollment.provenance),
        completed_event_id=completed_event_id,
    )


def _issued_criteria(
    db: Session, *, enrollment: CompletionEnrollment
) -> tuple[IssuedCompletionCriterion, ...]:
    revision = _criteria_head(db, enrollment)
    return tuple(
        IssuedCompletionCriterion(
            id=row.id,
            order=row.order,
            statement=row.statement,
            verification_method=row.verification_method,
        )
        for row in db.scalars(
            select(CompletionCriterion)
            .where(CompletionCriterion.revision_id == revision.revision_id)
            .order_by(CompletionCriterion.order, CompletionCriterion.id)
        ).all()
    )


def _policy_snapshot(
    db: Session, *, scope: CompletionPolicyScope, publish: bool
) -> CompletionPolicySnapshot:
    kind, identity, _, _ = _scope(db, scope, lock=False)
    head = _policy_head(db, kind, identity)
    policy = db.get(CompletionReviewPolicy, head.revision_id) if head else None
    return CompletionPolicySnapshot(
        scope=scope,
        revision_id=policy.id if policy else None,
        generation=head.generation if head else 0,
        independent_reviewer_required=policy.independent_reviewer_required
        if policy
        else None,
        reason=policy.reason if policy else None,
        publish_readiness=_readiness(
            "publish_policy",
            str(identity),
            None if publish else "write_preview_required",
        ),
    )


def policy_snapshot(
    db: Session, *, query: CompletionPolicyQuery
) -> CompletionPolicySnapshot:
    publish = query.purpose == CompletionPolicyPurpose.publish_policy
    permission = WRITE if publish else READ
    actor = revalidate_staff_admission(
        db, admission=query.access, permission=permission, write=publish
    )
    _scope(db, query.scope, lock=True)
    result = _policy_snapshot(db, scope=query.scope, publish=publish)
    if (
        revalidate_staff_admission(
            db, admission=query.access, permission=permission, write=publish
        )
        != actor
    ):
        raise _error("denied")
    return result


def workspace(db: Session, *, query: CompletionWorkspaceQuery) -> CompletionWorkspace:
    write = query.purpose != CompletionWorkspacePurpose.read
    permission = WRITE if write else READ
    actor = revalidate_staff_admission(
        db, admission=query.access, permission=permission, write=write
    )
    observed = db.scalar(
        select(WorkOrder).where(WorkOrder.public_id == query.work_order_public_id)
    )
    if observed is None:
        raise _error("not_found")
    enrollment = db.scalar(
        select(CompletionEnrollment).where(
            CompletionEnrollment.work_order_id == observed.id
        )
    )
    actual = db.scalar(
        select(ProjectWorkOrderIssuance).where(
            ProjectWorkOrderIssuance.work_order_id == observed.id
        )
    )
    engineering_stale = False
    linkage_blocker: str | None = None
    if enrollment:
        result, engineering_stale = _current(
            db,
            query=CompletionReviewQuery(
                access=query.access, enrollment_id=enrollment.id
            ),
            permission=permission,
            check_engineering=query.purpose == CompletionWorkspacePurpose.decide,
            skip_engineering=query.purpose != CompletionWorkspacePurpose.decide
            and query.purpose != CompletionWorkspacePurpose.read,
        )
        result = _decorate(db, result)
        row = db.get(WorkOrder, enrollment.work_order_id, populate_existing=True)
        assert row is not None
        records = (
            _history(db, enrollment=enrollment)
            if not write
            else _history(db, enrollment=enrollment, submission_id=result.submission_id)
            if result.submission_id
            else CompletionReviewHistory(
                enrollment_id=enrollment.id, submissions=(), decisions=()
            )
        )
        criteria = _issued_criteria(db, enrollment=enrollment)
        scope = _enrollment_scope(enrollment)
        provenance = CompletionProvenance(enrollment.provenance)
        actual_id = enrollment.engineering_issuance_id
    else:
        if query.purpose == CompletionWorkspacePurpose.enroll:
            try:
                preview_completion_enrollment_linkage(
                    db,
                    query=CompletionEnrollmentLinkagePreviewQuery(
                        access=query.access,
                        work_order_id=observed.id,
                        origin_ticket_id=observed.origin_ticket_id,
                    ),
                )
            except WorkOrderCommandError as exc:
                linkage_blocker = exc.code
        lock_work_order_linkage(
            db,
            query=WorkOrderLinkageFence(
                ticket_id=observed.origin_ticket_id, work_order_id=observed.id
            ),
        )
        if observed.project_id:
            engineering_plan_revocation_scope(
                db, project_id=observed.project_id, task_id=observed.project_task_id
            )
        row = db.scalar(
            select(WorkOrder)
            .where(WorkOrder.id == observed.id)
            .with_for_update()
            .execution_options(populate_existing=True)
        )
        if row is None:
            raise _error("not_found")
        result, records, criteria = _empty(), None, ()
        provenance = (
            CompletionProvenance.engineering_issuance
            if actual
            else CompletionProvenance.native_work_order
        )
        actual_id = actual.id if actual else None
        scope = (
            CompletionPolicyScope(
                kind=CompletionPolicyScopeKind.project, project_id=actual.project_id
            )
            if actual
            else CompletionPolicyScope(
                kind=CompletionPolicyScopeKind.work_order,
                work_order_public_id=row.public_id,
            )
        )
    policy = _policy_snapshot(db, scope=scope, publish=False)
    assignment = work_order_commands.current_assignment(
        db, query=CurrentWorkOrderAssignmentQuery(row.id)
    )
    submission = (
        db.get(CompletionSubmission, result.submission_id)
        if result.submission_id
        else None
    )
    decision = (
        db.get(CompletionReviewDecision, result.decision_id)
        if result.decision_id
        else None
    )
    event = _completed(db, row)
    decide_blocker: str | None = "write_preview_required"
    if query.purpose == CompletionWorkspacePurpose.decide:
        decide_blocker = (
            "enrollment_required"
            if enrollment is None
            else "scope_mismatch"
            if not _scope_matches(row, enrollment)
            else "engineering_stale"
            if engineering_stale
            else "policy_required"
            if policy.revision_id is None
            else "submission_required"
            if submission is None
            else "decision_exists"
            if decision is not None
            else "stale_state"
            if not _submission_current(db, enrollment, submission, row)
            else "independent_reviewer_required"
            if policy.independent_reviewer_required
            and (
                submission.actor_person_id is None
                or submission.actor_user_id == actor.system_user_id
                or submission.actor_person_id == actor.person_id
            )
            else None
        )
    revoke_blocker: str | None = "write_preview_required"
    if query.purpose == CompletionWorkspacePurpose.revoke:
        revoke_blocker = (
            "enrollment_required"
            if enrollment is None
            else "decision_required"
            if decision is None
            else "already_revoked"
            if decision.state == ReviewState.revoked
            else None
        )
    enroll_blocker: str | None = "write_preview_required"
    if query.purpose == CompletionWorkspacePurpose.enroll:
        enroll_blocker = (
            "already_enrolled"
            if enrollment
            else linkage_blocker
            if linkage_blocker
            else "policy_required"
            if policy.revision_id is None
            else None
        )
    criteria_blocker: str | None = "write_preview_required"
    if query.purpose == CompletionWorkspacePurpose.publish_criteria:
        criteria_blocker = (
            "enrollment_required"
            if enrollment is None
            else "provenance_mismatch"
            if provenance != CompletionProvenance.native_work_order
            else None
        )
    if (
        revalidate_staff_admission(
            db, admission=query.access, permission=permission, write=write
        )
        != actor
    ):
        raise _error("denied")
    return CompletionWorkspace(
        work_order_id=row.public_id,
        project_id=row.project_id,
        task_id=row.project_task_id,
        enrollment_id=enrollment.id if enrollment else None,
        engineering_issuance_id=actual_id,
        provenance=provenance,
        criteria_revision_id=_criteria_head(db, enrollment).revision_id
        if enrollment
        else None,
        criteria_generation=_criteria_head(db, enrollment).generation
        if enrollment
        else 0,
        completed_event_id=event.id if event else None,
        assignment_generation=assignment.generation if assignment else None,
        current=result,
        history=records if not write else None,
        current_submission=next(
            (item for item in records.submissions if item.id == result.submission_id),
            None,
        )
        if records
        else None,
        current_decision=next(
            (item for item in records.decisions if item.id == result.decision_id), None
        )
        if records
        else None,
        policy=policy,
        criteria=criteria,
        decide_readiness=_readiness("decide", row.public_id, decide_blocker),
        revoke_readiness=_readiness("revoke", row.public_id, revoke_blocker),
        enroll_readiness=_readiness("enroll", row.public_id, enroll_blocker),
        publish_criteria_readiness=_readiness(
            "publish_criteria", row.public_id, criteria_blocker
        ),
    )


def field_submission_preview(
    db: Session, *, query: FieldSubmissionPreviewQuery
) -> FieldSubmissionPreview:
    actor = resolve_field_actor(db, ResolveFieldActor(query.requester_system_user_id))
    initial = require_work_order(
        db, FieldWorkOrderScope(actor, query.work_order_public_id)
    )
    enrollment = db.scalar(
        select(CompletionEnrollment).where(
            CompletionEnrollment.work_order_id == initial.id
        )
    )
    db.scalar(
        select(SystemUser)
        .where(SystemUser.id == actor.system_user_id)
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    if actor.technician_id:
        db.scalar(
            select(TechnicianProfile)
            .where(TechnicianProfile.id == actor.technician_id)
            .with_for_update()
            .execution_options(populate_existing=True)
        )
    if enrollment:
        _origin_locks(db, enrollment)
    row = require_work_order(
        db, FieldWorkOrderScope(actor, query.work_order_public_id, lock=True)
    )
    assignment = work_order_commands.current_assignment(
        db, query=CurrentWorkOrderAssignmentQuery(row.id)
    )
    event = _completed(db, row)
    criteria_head = _criteria_head(db, enrollment) if enrollment else None
    policy = (
        _policy_snapshot(db, scope=_enrollment_scope(enrollment), publish=False)
        if enrollment
        else None
    )
    head = (
        db.scalar(
            select(CompletionReviewHead)
            .where(CompletionReviewHead.enrollment_id == enrollment.id)
            .with_for_update()
            .execution_options(populate_existing=True)
        )
        if enrollment
        else None
    )
    decision = (
        db.get(CompletionReviewDecision, head.decision_id)
        if head and head.decision_id
        else None
    )
    blocker = (
        "enrollment_required"
        if enrollment is None
        else "scope_mismatch"
        if not _scope_matches(row, enrollment)
        else "assignment_required"
        if assignment is None or assignment.generation is None
        else "completion_required"
        if event is None
        or event.assignment_generation != assignment.generation
        or event.system_user_id != actor.system_user_id
        or event.author_vendor_user_id != actor.vendor_user_id
        or event.author_technician_id != actor.technician_id
        else "policy_required"
        if policy is None or policy.revision_id is None
        else None
    )
    fresh_actor = resolve_field_actor(
        db, ResolveFieldActor(query.requester_system_user_id)
    )
    require_work_order(
        db, FieldWorkOrderScope(fresh_actor, query.work_order_public_id, lock=True)
    )
    if (
        fresh_actor != actor
        or work_order_commands.current_assignment(
            db, query=CurrentWorkOrderAssignmentQuery(row.id)
        )
        != assignment
    ):
        raise _error("denied")
    return FieldSubmissionPreview(
        work_order_id=row.public_id,
        enrollment_id=enrollment.id if enrollment else None,
        engineering_issuance_id=enrollment.engineering_issuance_id
        if enrollment
        else None,
        provenance=CompletionProvenance(enrollment.provenance) if enrollment else None,
        criteria_revision_id=criteria_head.revision_id if criteria_head else None,
        criteria_generation=criteria_head.generation if criteria_head else 0,
        completed_event_id=event.id if event else None,
        assignment_generation=assignment.generation if assignment else None,
        generation=head.generation if head else 0,
        policy_revision_id=policy.revision_id if policy else None,
        review_state=ReviewState(decision.state)
        if decision
        else ReviewState.submitted
        if head
        else ReviewState.legacy_not_reviewed,
        criteria=_issued_criteria(db, enrollment=enrollment) if enrollment else (),
        submission_readiness=_readiness("submit_evidence", row.public_id, blocker),
    )


def evidence_content(
    db: Session, *, query: CompletionEvidenceQuery
) -> CompletionEvidenceContent:
    if query.purpose not in {
        CompletionWorkspacePurpose.read,
        CompletionWorkspacePurpose.decide,
    }:
        raise _error("invalid_request")
    write = query.purpose == CompletionWorkspacePurpose.decide
    permission = WRITE if write else READ
    actor = revalidate_staff_admission(
        db, admission=query.access, permission=permission, write=write
    )
    enrollment = _enrollment(db, query.enrollment_id)
    _origin_locks(db, enrollment)
    row = db.scalar(
        select(WorkOrder)
        .where(WorkOrder.id == enrollment.work_order_id)
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    head = db.scalar(
        select(CompletionReviewHead)
        .where(CompletionReviewHead.enrollment_id == enrollment.id)
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    if write and (
        head is None
        or head.submission_id != query.submission_id
        or head.decision_id is not None
        or row is None
        or not _scope_matches(row, enrollment)
    ):
        raise _error("denied")
    submission = db.get(CompletionSubmission, query.submission_id)
    if submission is None or submission.enrollment_id != enrollment.id:
        raise _error("not_found")
    retained = db.scalars(
        select(CompletionEvidenceFile).where(
            CompletionEvidenceFile.submission_id == submission.id,
            CompletionEvidenceFile.attachment_id == query.attachment_id,
        )
    ).first()
    if retained is None:
        raise _error("not_found")
    if (
        revalidate_staff_admission(
            db, admission=query.access, permission=permission, write=write
        )
        != actor
    ):
        raise _error("denied")
    content = stream_retained_evidence(
        db,
        query=RetainedEvidenceStreamQuery(
            stored_file_id=retained.stored_file_id,
            submission_id=submission.id,
            expected_checksum=retained.checksum,
        ),
    )
    if (
        revalidate_staff_admission(
            db, admission=query.access, permission=permission, write=write
        )
        != actor
    ):
        raise _error("denied")
    return CompletionEvidenceContent(
        evidence=CompletionEvidenceDisplay(
            attachment_id=query.attachment_id,
            filename=content.filename,
            content_type=content.content_type,
            file_size=content.file_size,
            checksum=content.checksum,
        ),
        stream=content.stream,
    )


def linked_enrollments(
    db: Session, *, query: LinkedTicketEnrollmentQuery
) -> LinkedTicketEnrollments:
    lock_work_order_linkage(db, query=WorkOrderLinkageFence(ticket_id=query.ticket_id))
    ids = db.scalars(
        select(WorkOrder.id)
        .join(CompletionEnrollment, CompletionEnrollment.work_order_id == WorkOrder.id)
        .where(WorkOrder.origin_ticket_id == query.ticket_id)
        .order_by(WorkOrder.id)
    ).all()
    return LinkedTicketEnrollments(ticket_id=query.ticket_id, work_order_ids=tuple(ids))


def stage_ticket_completion_basis(
    db: Session, *, query: TicketCompletionBasisQuery
) -> TicketCompletionBasis:
    if not owner_command_active(db, owner="support.ticket_lifecycle"):
        raise _error("participant_required")
    return _ticket_completion_basis_locked(db, ticket_id=query.ticket_id, access=None)


def preview_ticket_completion_basis(
    db: Session, *, query: SupportTicketCompletionPreviewQuery
) -> TicketCompletionBasis:
    actor = revalidate_staff_admission(
        db, admission=query.access, permission="support:ticket:update", write=True
    )
    from app.services.topology.outage_completion import lock_complaint_graph
    from app.services.topology.outage_completion_contracts import (
        ComplaintGraphLocksQuery,
    )

    lock_complaint_graph(db, query=ComplaintGraphLocksQuery((query.ticket_id,)))
    result = _ticket_completion_basis_locked(
        db, ticket_id=query.ticket_id, access=query.access
    )
    if (
        revalidate_staff_admission(
            db, admission=query.access, permission="support:ticket:update", write=True
        )
        != actor
    ):
        raise _error("denied")
    return result


def _ticket_completion_basis_locked(
    db: Session, *, ticket_id: UUID, access: PermissionAdmission | None
) -> TicketCompletionBasis:
    from app.services.topology.outage_completion import (
        has_complaint_links,
        preview_complaint_resolution_scopes,
        stage_complaint_resolution_scopes,
    )
    from app.services.topology.outage_completion_contracts import (
        ComplaintResolutionScopesPreviewQuery,
        ComplaintResolutionScopesQuery,
    )

    shared = (
        (
            stage_complaint_resolution_scopes(
                db, query=ComplaintResolutionScopesQuery(ticket_id)
            )
            if access is None
            else preview_complaint_resolution_scopes(
                db, query=ComplaintResolutionScopesPreviewQuery(ticket_id, access)
            )
        ).incidents
        if has_complaint_links(db, query=ComplaintResolutionScopesQuery(ticket_id))
        else ()
    )
    native = tuple(
        db.scalars(
            select(CompletionEnrollment.work_order_id)
            .join(WorkOrder, WorkOrder.id == CompletionEnrollment.work_order_id)
            .where(WorkOrder.origin_ticket_id == ticket_id)
        ).all()
    )
    required = tuple(
        sorted(
            set(native)
            | {proof.work_order_id for scope in shared for proof in scope.issuances}
        )
    )
    result = _scoped_completion_basis_locked(
        db,
        required_work_order_ids=required,
        access=access,
        participant=CompletionBasisParticipant.support_confirmation,
        expected_enrollment_ids=None,
    )
    return TicketCompletionBasis(ticket_id, result.entries, shared)


def stage_scoped_completion_basis(
    db: Session, *, query: ScopedCompletionBasisQuery
) -> ScopedCompletionBasis:
    owner = (
        "support.ticket_lifecycle"
        if query.participant == CompletionBasisParticipant.support_confirmation
        else "operations.field_completion"
    )
    if not owner_command_active(db, owner=owner):
        raise _error("participant_required")
    return _scoped_completion_basis_locked(
        db,
        required_work_order_ids=query.required_work_order_ids,
        participant=query.participant,
        expected_enrollment_ids=query.expected_enrollment_ids,
        access=None,
    )


def preview_scoped_completion_basis(
    db: Session, *, query: ScopedCompletionBasisPreviewQuery
) -> ScopedCompletionBasis:
    permission = (
        "support:ticket:update"
        if query.participant == CompletionBasisParticipant.support_confirmation
        else "project:task:write"
        if query.participant == CompletionBasisParticipant.project_task_completion
        else "project:update"
    )
    actor = revalidate_staff_admission(
        db, admission=query.access, permission=permission, write=True
    )
    result = _scoped_completion_basis_locked(
        db,
        required_work_order_ids=query.required_work_order_ids,
        participant=query.participant,
        expected_enrollment_ids=query.expected_enrollment_ids,
        access=query.access,
    )
    if (
        revalidate_staff_admission(
            db, admission=query.access, permission=permission, write=True
        )
        != actor
    ):
        raise _error("denied")
    return result


def _scoped_completion_basis_locked(
    db: Session,
    *,
    required_work_order_ids: tuple[UUID, ...],
    participant: CompletionBasisParticipant,
    expected_enrollment_ids: tuple[UUID, ...] | None,
    access: PermissionAdmission | None,
) -> ScopedCompletionBasis:
    required_ids = set(required_work_order_ids)
    required_enrolled = tuple(
        db.scalars(
            select(CompletionEnrollment)
            .where(CompletionEnrollment.work_order_id.in_(required_ids))
            .order_by(CompletionEnrollment.work_order_id)
        ).all()
    )
    if {item.work_order_id for item in required_enrolled} != required_ids:
        raise _error("enrollment_required")
    if expected_enrollment_ids is not None and {
        item.id for item in required_enrolled
    } != set(expected_enrollment_ids):
        raise _error("scope_mismatch")
    # Replacement edges cannot cross this exact immutable enrollment scope.
    # Never expand to unrelated projects merely because they share a ticket.
    if not required_ids:
        return ScopedCompletionBasis(())
    graph = [
        and_(
            CompletionEnrollment.origin_ticket_id == item.origin_ticket_id,
            CompletionEnrollment.subscriber_id == item.subscriber_id,
            CompletionEnrollment.project_id == item.project_id,
            CompletionEnrollment.task_id == item.task_id,
            CompletionEnrollment.provenance == item.provenance,
        )
        for item in required_enrolled
    ]
    enrolled = tuple(
        db.scalars(
            select(CompletionEnrollment)
            .where(or_(*graph))
            .order_by(CompletionEnrollment.work_order_id)
        ).all()
    )
    for project_id in sorted(
        {item.project_id for item in enrolled if item.project_id is not None}
    ):
        engineering_plan_revocation_scope(db, project_id=project_id, task_id=None)
    for item in sorted(
        (item for item in enrolled if item.task_id is not None),
        key=lambda item: (str(item.project_id), str(item.task_id)),
    ):
        _origin_locks(db, item)

    from app.services.project_engineering_plan_contracts import (
        SupportCompletionEngineeringBatchQuery,
        SupportCompletionEngineeringPreviewBatchQuery,
        SupportCompletionEngineeringQuery,
    )
    from app.services.project_engineering_plans import (
        preview_current_batch_for_support_completion,
        stage_current_batch_for_support_completion,
    )

    issued = tuple(
        db.get(ProjectWorkOrderIssuance, item.engineering_issuance_id)
        for item in enrolled
        if item.engineering_issuance_id is not None
    )
    if any(item is None for item in issued):
        raise _error("provenance_mismatch")
    engineering_query = SupportCompletionEngineeringBatchQuery(
        plans=tuple(
            SupportCompletionEngineeringQuery(
                plan_id=item.plan_id,
                project_id=item.project_id,
                task_id=item.task_id,
                expected_revision_id=item.revision_id,
                expected_approval_id=item.approval_id,
                expected_digest=item.digest,
            )
            for item in issued
            if item is not None
        )
    )
    if participant == CompletionBasisParticipant.support_confirmation:
        if access is None:
            stage_current_batch_for_support_completion(db, query=engineering_query)
        else:
            preview_current_batch_for_support_completion(
                db,
                query=SupportCompletionEngineeringPreviewBatchQuery(
                    plans=engineering_query.plans, access=access
                ),
            )
    else:
        from app.services.project_engineering_plan_contracts import (
            AcceptedProjectCompletionEngineeringPreviewBatchQuery,
            AcceptedProjectCompletionPurpose,
        )
        from app.services.project_engineering_plans import (
            preview_current_batch_for_accepted_project_completion,
            stage_current_batch_for_accepted_project_completion,
        )

        if access is None:
            stage_current_batch_for_accepted_project_completion(
                db, query=engineering_query
            )
        else:
            preview_current_batch_for_accepted_project_completion(
                db,
                query=AcceptedProjectCompletionEngineeringPreviewBatchQuery(
                    plans=engineering_query.plans,
                    access=access,
                    purpose=AcceptedProjectCompletionPurpose.task
                    if participant == CompletionBasisParticipant.project_task_completion
                    else AcceptedProjectCompletionPurpose.project,
                ),
            )
    orders = {item.id: _work_order(db, item) for item in enrolled}
    for item in sorted(enrolled, key=lambda item: item.id):
        _criteria_head(db, item)
    for kind, identity in sorted(
        {(item.policy_scope_type, item.policy_scope_id) for item in enrolled}
    ):
        _policy_head(db, kind, identity)
    heads = {
        item.id: db.scalar(
            select(CompletionReviewHead)
            .where(CompletionReviewHead.enrollment_id == item.id)
            .with_for_update()
            .execution_options(populate_existing=True)
        )
        for item in sorted(enrolled, key=lambda item: item.id)
    }
    replacement_heads = {
        item.source_enrollment_id: item
        for item in db.scalars(
            select(CompletionReplacementHead)
            .where(
                CompletionReplacementHead.source_enrollment_id.in_(
                    tuple(item.id for item in enrolled)
                )
            )
            .order_by(CompletionReplacementHead.source_enrollment_id)
            .with_for_update()
            .execution_options(populate_existing=True)
        ).all()
    }
    by_id = {item.id: item for item in enrolled}
    entries = []
    for required in required_enrolled:
        item = required
        lineage: list[UUID] = []
        visited: set[UUID] = set()
        while True:
            if item.id in visited:
                raise _error("replacement_cycle")
            visited.add(item.id)
            replacement_head = replacement_heads.get(item.id)
            if (
                replacement_head is None
                or replacement_head.replacement_enrollment_id is None
            ):
                break
            replacement_decision = db.get(
                CompletionReplacementDecision, replacement_head.decision_id
            )
            successor = by_id.get(replacement_head.replacement_enrollment_id)
            if (
                replacement_decision is None
                or successor is None
                or replacement_decision.state != ReplacementState.approved
                or replacement_decision.source_enrollment_id != item.id
                or replacement_decision.replacement_enrollment_id != successor.id
                or replacement_decision.generation != replacement_head.generation
            ):
                raise _error("replacement_stale")
            _replacement_fresh(
                db,
                decision=replacement_decision,
                source=item,
                replacement=successor,
                orders=orders,
                access=access,
                participant=participant,
            )
            lineage.append(replacement_decision.id)
            item = successor
        head = heads[item.id]
        submission = db.get(CompletionSubmission, head.submission_id) if head else None
        decision = (
            db.get(CompletionReviewDecision, head.decision_id)
            if head and head.decision_id
            else None
        )
        if (
            head is None
            or submission is None
            or decision is None
            or decision.state != ReviewState.accepted
            or not _submission_current(db, item, submission, orders[item.id])
        ):
            raise _error("completion_pending")
        entries.append(
            TicketCompletionBasisEntry(
                work_order_id=required.work_order_id,
                enrollment_id=required.id,
                criteria_revision_id=submission.criteria_revision_id,
                policy_revision_id=submission.policy_revision_id,
                assignment_generation=submission.assignment_generation,
                completed_event_id=submission.completed_event_id,
                submission_id=submission.id,
                accepted_decision_id=decision.id,
                review_generation=head.generation,
                effective_work_order_id=item.work_order_id if lineage else None,
                effective_enrollment_id=item.id if lineage else None,
                replacement_decision_ids=tuple(lineage),
            )
        )
    return ScopedCompletionBasis(entries=tuple(entries))


def review_failed_scope_replacement(
    db: Session, *, command: ReviewFailedScopeReplacement
) -> FailedScopeReplacementOutcome:
    return _execute_replacement(db, command=command)


def revoke_failed_scope_replacement(
    db: Session, *, command: RevokeFailedScopeReplacement
) -> FailedScopeReplacementOutcome:
    return _execute_replacement(db, command=command)


def _execute_replacement(
    db: Session, *, command: ReplacementCommand
) -> FailedScopeReplacementOutcome:
    return _run_owner(
        db,
        command=command,
        concern=REPLACEMENT_CONCERN,
        operation=lambda: _stage_replacement(db, command=command),
    )


def _replacement_outcome(
    decision: CompletionReplacementDecision, *, replayed: bool = False
) -> FailedScopeReplacementOutcome:
    return FailedScopeReplacementOutcome(
        source_enrollment_id=decision.source_enrollment_id,
        replacement_enrollment_id=decision.replacement_enrollment_id,
        decision_id=decision.id,
        generation=decision.generation,
        state=ReplacementState(decision.state),
        replayed=replayed,
    )


def _replacement_scope(
    source: CompletionEnrollment, replacement: CompletionEnrollment
) -> ReplacementScope:
    if source.id == replacement.id:
        raise _error("replacement_cycle")
    if (
        source.origin_ticket_id,
        source.subscriber_id,
        source.project_id,
        source.task_id,
        source.provenance,
    ) != (
        replacement.origin_ticket_id,
        replacement.subscriber_id,
        replacement.project_id,
        replacement.task_id,
        replacement.provenance,
    ):
        raise _error("scope_mismatch")
    if source.provenance == CompletionProvenance.native_work_order:
        if source.origin_ticket_id is None:
            raise _error("scope_mismatch")
        if source.subscriber_id is None:
            return OutageInfrastructureReplacementScope(source.origin_ticket_id)
        return NativeTicketReplacementScope(
            source.origin_ticket_id, source.subscriber_id
        )
    if (
        source.provenance == CompletionProvenance.engineering_issuance
        and source.project_id is not None
    ):
        if (
            source.engineering_issuance_id is None
            or replacement.engineering_issuance_id is None
        ):
            raise _error("engineering_replacement_proof_required")
        return EngineeringReplacementScope(source.project_id, source.task_id)
    raise _error("provenance_mismatch")


def _replacement_graph(
    db: Session, *, source: CompletionEnrollment
) -> tuple[tuple[CompletionEnrollment, ...], bool]:
    """Fence the correct native-ticket or engineering-project/task graph."""
    from app.models.support import Ticket

    if source.provenance == CompletionProvenance.native_work_order:
        if source.origin_ticket_id is None:
            raise _error("scope_mismatch")
        from app.services.topology.outage_completion import lock_complaint_graph
        from app.services.topology.outage_completion_contracts import (
            ComplaintGraphLocksQuery,
        )

        if source.subscriber_id is None:
            from app.services.field.completion_outage_scope import (
                prepare_outage_replacement_graph,
            )

            prepare_outage_replacement_graph(db, work_order_id=source.work_order_id)
        else:
            lock_complaint_graph(
                db, query=ComplaintGraphLocksQuery((source.origin_ticket_id,))
            )
        ticket = db.scalar(
            select(Ticket)
            .where(Ticket.id == source.origin_ticket_id)
            .with_for_update()
            .execution_options(populate_existing=True)
        )
        enrolled = tuple(
            db.scalars(
                select(CompletionEnrollment)
                .where(
                    CompletionEnrollment.origin_ticket_id == source.origin_ticket_id,
                    CompletionEnrollment.subscriber_id == source.subscriber_id,
                    CompletionEnrollment.project_id == source.project_id,
                    CompletionEnrollment.task_id == source.task_id,
                    CompletionEnrollment.provenance == source.provenance,
                )
                .order_by(CompletionEnrollment.work_order_id)
            ).all()
        )
        terminal = ticket is None or ticket.status in {
            "closed",
            "resolved",
            "canceled",
            "merged",
        }
    elif (
        source.provenance == CompletionProvenance.engineering_issuance
        and source.project_id is not None
    ):
        query = (
            select(CompletionEnrollment)
            .where(
                CompletionEnrollment.project_id == source.project_id,
                CompletionEnrollment.task_id == source.task_id,
                CompletionEnrollment.provenance
                == CompletionProvenance.engineering_issuance.value,
            )
            .order_by(CompletionEnrollment.work_order_id)
        )
        observed = tuple(db.scalars(query).all())
        # Enrollment takes its WO linkage fence before Project. Match that order.
        for item in observed:
            lock_work_order_linkage(
                db,
                query=WorkOrderLinkageFence(
                    ticket_id=item.origin_ticket_id, work_order_id=item.work_order_id
                ),
            )
        engineering_plan_revocation_scope(
            db, project_id=source.project_id, task_id=None
        )
        _origin_locks(db, source)
        enrolled = tuple(db.scalars(query).all())
        terminal = False
    else:
        raise _error("provenance_mismatch")
    for project_id in sorted(
        {item.project_id for item in enrolled if item.project_id is not None}
    ):
        engineering_plan_revocation_scope(db, project_id=project_id, task_id=None)
    for item in sorted(
        enrolled, key=lambda item: (str(item.project_id), str(item.task_id))
    ):
        _origin_locks(db, item)
    return enrolled, terminal


def _engineering_replacement_proof(
    db: Session,
    *,
    source: CompletionEnrollment,
    replacement: CompletionEnrollment,
    access: PermissionAdmission,
    purpose: str,
) -> None:
    scope = _replacement_scope(source, replacement)
    if isinstance(scope, NativeTicketReplacementScope):
        return
    if isinstance(scope, OutageInfrastructureReplacementScope):
        from app.services.field.completion_outage_scope import review_outage_replacement

        review_outage_replacement(
            db, source=source, replacement=replacement, access=access, purpose=purpose
        )
        return
    from app.services.project_work_order_handoff import completion_issuance_evidence
    from app.services.project_work_order_handoff_contracts import (
        CompletionIssuanceEvidenceQuery,
        CompletionIssuancePurpose,
    )

    receipts = []
    for item in (source, replacement):
        if item.engineering_issuance_id is None:
            raise _error("engineering_replacement_proof_required")
        receipt = completion_issuance_evidence(
            db,
            query=CompletionIssuanceEvidenceQuery(
                access=access,
                purpose=CompletionIssuancePurpose.read
                if purpose == "read"
                else CompletionIssuancePurpose.review,
                issuance_id=item.engineering_issuance_id,
                expected_work_order_id=item.work_order_id,
                expected_public_id=item.public_id,
            ),
        )
        if receipt.project_id != item.project_id or receipt.task_id != item.task_id:
            raise _error("provenance_mismatch")
        receipts.append(receipt)
    first, second = receipts
    if (
        first.project_id,
        first.task_id,
        first.plan_id,
        first.revision_id,
        first.approval_id,
        first.digest,
        first.infrastructure,
        first.purpose,
        first.technology,
    ) != (
        second.project_id,
        second.task_id,
        second.plan_id,
        second.revision_id,
        second.approval_id,
        second.digest,
        second.infrastructure,
        second.purpose,
        second.technology,
    ):
        raise _error("engineering_replacement_proof_required")
    for item in (source, replacement):
        _engineering_current(db, enrollment=item, access=access, purpose=purpose)


def _engineering_replacement_criteria(
    db: Session,
    *,
    source: CompletionEnrollment,
    replacement: CompletionEnrollment,
    decision: CompletionReplacementDecision,
    coverage: tuple[CriterionCoverage, ...],
) -> None:
    if source.provenance != CompletionProvenance.engineering_issuance:
        return
    source_receipt = db.get(ProjectWorkOrderIssuance, source.engineering_issuance_id)
    replacement_receipt = db.get(
        ProjectWorkOrderIssuance, replacement.engineering_issuance_id
    )
    if source_receipt is None or replacement_receipt is None:
        raise _error("engineering_replacement_proof_required")
    expected = tuple(
        (item.id, item.order, item.statement, item.verification_method)
        for item in db.scalars(
            select(EngineeringPlanCriterion)
            .where(EngineeringPlanCriterion.revision_id == source_receipt.revision_id)
            .order_by(EngineeringPlanCriterion.order)
        ).all()
    )
    if not expected or source_receipt.revision_id != replacement_receipt.revision_id:
        raise _error("engineering_replacement_proof_required")
    for item, revision_id in (
        (source, decision.source_criteria_revision_id),
        (replacement, decision.replacement_criteria_revision_id),
    ):
        revision = db.get(CompletionCriteriaRevision, revision_id)
        actual = tuple(
            (
                criterion.id,
                criterion.order,
                criterion.statement,
                criterion.verification_method,
            )
            for criterion in db.scalars(
                select(CompletionCriterion)
                .where(CompletionCriterion.revision_id == revision_id)
                .order_by(CompletionCriterion.order)
            ).all()
        )
        if (
            revision is None
            or revision.enrollment_id != item.id
            or revision.engineering_revision_id != source_receipt.revision_id
            or actual != expected
        ):
            raise _error("engineering_replacement_proof_required")
        row = db.get(WorkOrder, item.work_order_id, populate_existing=True)
        if (
            row is None
            or not _scope_matches(row, item)
            or row.purpose != source_receipt.purpose
            or row.access_technology != source_receipt.technology
            or row.work_order_kind != "infrastructure"
        ):
            raise _error("provenance_mismatch")
    if any(
        item.source_criterion_id != item.replacement_criterion_id for item in coverage
    ):
        raise _error("criterion_mismatch")


def _replacement_assignments(
    db: Session, *, decision: CompletionReplacementDecision
) -> None:
    for identity, generation in (
        (decision.source_enrollment_id, decision.source_assignment_generation),
        (
            decision.replacement_enrollment_id,
            decision.replacement_assignment_generation,
        ),
    ):
        enrollment = _enrollment(db, identity)
        assignment = work_order_commands.current_assignment(
            db, query=CurrentWorkOrderAssignmentQuery(enrollment.work_order_id)
        )
        if assignment is None or assignment.generation != generation:
            raise _error("assignment_generation_mismatch")


def _replacement_failure(
    db: Session,
    *,
    decision: CompletionReplacementDecision,
    source: CompletionEnrollment,
    order: WorkOrder,
) -> None:
    if decision.failure_event_id is not None:
        event = db.scalar(
            select(FieldJobEvent)
            .where(FieldJobEvent.work_order_mirror_id == order.id)
            .order_by(FieldJobEvent.received_at.desc(), FieldJobEvent.id.desc())
            .limit(1)
        )
        if (
            event is None
            or event.id != decision.failure_event_id
            or event.event != "unable_to_complete"
            or event.new_status != "canceled"
            or order.status != "canceled"
            or event.assignment_generation != decision.source_assignment_generation
        ):
            raise _error("failure_basis_stale")
    else:
        head = db.get(CompletionReviewHead, source.id, populate_existing=True)
        rejected = db.get(CompletionReviewDecision, decision.failure_decision_id)
        submission = db.get(CompletionSubmission, decision.failure_submission_id)
        if (
            head is None
            or rejected is None
            or submission is None
            or head.generation != decision.failure_review_generation
            or head.decision_id != rejected.id
            or head.submission_id != submission.id
            or rejected.state != ReviewState.rejected
            or rejected.enrollment_id != source.id
            or rejected.submission_id != submission.id
            or not _submission_current(db, source, submission, order)
        ):
            raise _error("failure_basis_stale")


def _replacement_coverage(
    db: Session,
    *,
    source: CompletionEnrollment,
    replacement: CompletionEnrollment,
    decision: CompletionReplacementDecision,
    coverage: tuple[CriterionCoverage, ...],
) -> None:
    if (
        _criteria_head(db, source).revision_id != decision.source_criteria_revision_id
        or _criteria_head(db, replacement).revision_id
        != decision.replacement_criteria_revision_id
    ):
        raise _error("stale_criteria")
    _exact_criteria(
        _criteria(db, decision.source_criteria_revision_id),
        tuple(item.source_criterion_id for item in coverage),
    )
    if len({item.replacement_criterion_id for item in coverage}) != len(coverage):
        raise _error("criterion_mismatch")
    _engineering_replacement_criteria(
        db, source=source, replacement=replacement, decision=decision, coverage=coverage
    )
    replacement_criteria = _criteria(db, decision.replacement_criteria_revision_id)
    if any(
        item.replacement_criterion_id not in replacement_criteria for item in coverage
    ):
        raise _error("criterion_mismatch")


def _replacement_fresh(
    db: Session,
    *,
    decision: CompletionReplacementDecision,
    source: CompletionEnrollment,
    replacement: CompletionEnrollment,
    orders: dict[UUID, WorkOrder],
    access: PermissionAdmission | None = None,
    participant: CompletionBasisParticipant | None = None,
) -> None:
    scope = _replacement_scope(source, replacement)
    if isinstance(scope, OutageInfrastructureReplacementScope):
        from app.services.field.completion_outage_scope import (
            validate_outage_replacement_proof,
        )
        from app.services.topology.outage_completion_contracts import (
            OutageCoverageParticipant,
        )

        validate_outage_replacement_proof(
            db,
            decision_id=decision.id,
            source=source,
            replacement=replacement,
            access=access,
            participant=OutageCoverageParticipant(participant.value)
            if participant
            else OutageCoverageParticipant.completion_review,
        )
    _replacement_assignments(db, decision=decision)
    _policy(db, enrollment=source, revision_id=decision.source_policy_revision_id)
    _policy(
        db, enrollment=replacement, revision_id=decision.replacement_policy_revision_id
    )
    coverage = tuple(
        CriterionCoverage(item.source_criterion_id, item.replacement_criterion_id)
        for item in db.scalars(
            select(CompletionReplacementCoverage).where(
                CompletionReplacementCoverage.decision_id == decision.id
            )
        ).all()
    )
    _replacement_coverage(
        db, source=source, replacement=replacement, decision=decision, coverage=coverage
    )
    _replacement_failure(db, decision=decision, source=source, order=orders[source.id])
    _replacement_independence(
        db,
        actor=AuthorizedStaffPerson(decision.actor_user_id, decision.actor_person_id),
        enrollments=(source, replacement),
        policies={
            source.id: _policy(
                db, enrollment=source, revision_id=decision.source_policy_revision_id
            ),
            replacement.id: _policy(
                db,
                enrollment=replacement,
                revision_id=decision.replacement_policy_revision_id,
            ),
        },
    )


def _replacement_independence(
    db: Session,
    *,
    actor: AuthorizedStaffPerson,
    enrollments: tuple[CompletionEnrollment, ...],
    policies: dict[UUID, CompletionReviewPolicy],
) -> None:
    for enrollment in enrollments:
        if not policies[enrollment.id].independent_reviewer_required:
            continue
        assignment = work_order_commands.current_assignment(
            db, query=CurrentWorkOrderAssignmentQuery(enrollment.work_order_id)
        )
        people: set[UUID] = set()
        users: set[UUID] = set()
        if assignment is not None:
            if assignment.technician_id is not None:
                technician = db.get(TechnicianProfile, assignment.technician_id)
                if technician is None or technician.system_user_id is None:
                    raise _error("independent_reviewer_required")
                users.add(technician.system_user_id)
            if assignment.vendor_user_id is not None:
                from app.models.field_vendor import FieldVendorUser

                vendor_user = db.get(FieldVendorUser, assignment.vendor_user_id)
                if vendor_user is None or vendor_user.system_user_id is None:
                    raise _error("independent_reviewer_required")
                users.add(vendor_user.system_user_id)
        for event in db.scalars(
            select(FieldJobEvent).where(
                FieldJobEvent.work_order_mirror_id == enrollment.work_order_id
            )
        ).all():
            if event.person_id is not None:
                people.add(event.person_id)
            if event.system_user_id is None:
                raise _error("independent_reviewer_required")
            users.add(event.system_user_id)
        for submission in db.scalars(
            select(CompletionSubmission).where(
                CompletionSubmission.enrollment_id == enrollment.id
            )
        ).all():
            if submission.actor_person_id is not None:
                people.add(submission.actor_person_id)
            if submission.actor_user_id is not None:
                users.add(submission.actor_user_id)
        if actor.person_id in people or actor.system_user_id in users:
            raise _error("independent_reviewer_required")
        for user_id in users:
            user = db.get(SystemUser, user_id)
            if (
                user is None
                or user.person_party_id is None
                or user.person_party_id == actor.person_id
            ):
                raise _error("independent_reviewer_required")


def _stage_replacement(
    db: Session, *, command: ReplacementCommand
) -> FailedScopeReplacementOutcome:
    key, fingerprint = _identity(db, command)
    actor = _staff(db, command)
    if (
        command.expected_work_order_public_id is not None
        and _enrollment(db, command.source_enrollment_id).public_id
        != command.expected_work_order_public_id
    ):
        raise _error("scope_mismatch")
    if db.get(CompletionReviewReceipt, key, populate_existing=True) is not None:
        raise _error("idempotency_conflict")
    receipt = db.get(CompletionReplacementReceipt, key, populate_existing=True)
    if receipt is not None:
        if receipt.fingerprint != fingerprint:
            raise _error("idempotency_conflict")
        decision = db.get(CompletionReplacementDecision, receipt.decision_id)
        if decision is None or _staff(db, command) != actor:
            raise _error("denied")
        return _replacement_outcome(decision, replayed=True)
    source = _enrollment(db, command.source_enrollment_id)
    previous = None
    if isinstance(command, RevokeFailedScopeReplacement):
        previous = db.get(CompletionReplacementDecision, command.decision_id)
        if previous is None or previous.source_enrollment_id != source.id:
            raise _error("stale_state")
        replacement = _enrollment(db, previous.replacement_enrollment_id)
    else:
        replacement = _enrollment(db, command.replacement_enrollment_id)
    _replacement_scope(source, replacement)
    enrolled, terminal = _replacement_graph(db, source=source)
    if terminal and isinstance(command, ReviewFailedScopeReplacement):
        raise _error("origin_ticket_terminal")
    if isinstance(command, ReviewFailedScopeReplacement):
        _engineering_replacement_proof(
            db,
            source=source,
            replacement=replacement,
            access=command.access,
            purpose="stage",
        )
    orders = {
        item.id: _historical_replacement_order(db, item)
        if isinstance(command, RevokeFailedScopeReplacement)
        else _work_order(db, item)
        for item in enrolled
    }
    for item in sorted(enrolled, key=lambda item: item.id):
        _criteria_head(db, item)
    for kind, identity in sorted(
        {(item.policy_scope_type, item.policy_scope_id) for item in enrolled}
    ):
        _policy_head(db, kind, identity)
    for item in sorted(enrolled, key=lambda item: item.id):
        db.scalar(
            select(CompletionReviewHead)
            .where(CompletionReviewHead.enrollment_id == item.id)
            .with_for_update()
            .execution_options(populate_existing=True)
        )
    heads = {
        item.source_enrollment_id: item
        for item in db.scalars(
            select(CompletionReplacementHead)
            .where(
                CompletionReplacementHead.source_enrollment_id.in_(
                    tuple(item.id for item in enrolled)
                )
            )
            .order_by(CompletionReplacementHead.source_enrollment_id)
            .with_for_update()
            .execution_options(populate_existing=True)
        ).all()
    }
    head = heads.get(source.id)
    if (head.generation if head else 0) != command.expected_generation:
        raise _error("stale_state")
    if isinstance(command, RevokeFailedScopeReplacement):
        if (
            previous is None
            or head is None
            or head.decision_id != previous.id
            or head.replacement_enrollment_id is None
        ):
            raise _error("stale_state")
        decision = CompletionReplacementDecision(
            source_enrollment_id=previous.source_enrollment_id,
            replacement_enrollment_id=previous.replacement_enrollment_id,
            source_criteria_revision_id=previous.source_criteria_revision_id,
            replacement_criteria_revision_id=previous.replacement_criteria_revision_id,
            source_policy_revision_id=previous.source_policy_revision_id,
            replacement_policy_revision_id=previous.replacement_policy_revision_id,
            source_assignment_generation=previous.source_assignment_generation,
            replacement_assignment_generation=previous.replacement_assignment_generation,
            failure_event_id=previous.failure_event_id,
            failure_submission_id=previous.failure_submission_id,
            failure_decision_id=previous.failure_decision_id,
            failure_review_generation=previous.failure_review_generation,
            generation=head.generation + 1,
            state=ReplacementState.revoked.value,
            revokes_id=previous.id,
            actor_user_id=actor.system_user_id,
            actor_person_id=actor.person_id,
            reason=command.context.reason,
        )
        coverage: tuple[CriterionCoverage, ...] = ()
    else:
        if head is not None and head.replacement_enrollment_id is not None:
            raise _error("replacement_conflict")
        if any(
            item.replacement_enrollment_id == replacement.id for item in heads.values()
        ):
            raise _error("replacement_conflict")
        visited = {source.id}
        cursor = replacement.id
        while True:
            if cursor in visited:
                raise _error("replacement_cycle")
            visited.add(cursor)
            successor = heads.get(cursor)
            if successor is None or successor.replacement_enrollment_id is None:
                break
            cursor = successor.replacement_enrollment_id
        failure = command.failure_basis
        decision = CompletionReplacementDecision(
            source_enrollment_id=source.id,
            replacement_enrollment_id=replacement.id,
            generation=command.expected_generation + 1,
            state=ReplacementState.approved.value,
            source_criteria_revision_id=command.source_criteria_revision_id,
            replacement_criteria_revision_id=command.replacement_criteria_revision_id,
            source_policy_revision_id=command.source_policy_revision_id,
            replacement_policy_revision_id=command.replacement_policy_revision_id,
            source_assignment_generation=command.source_assignment_generation,
            replacement_assignment_generation=command.replacement_assignment_generation,
            failure_event_id=failure.event_id
            if isinstance(failure, UnableToCompleteFailure)
            else None,
            failure_submission_id=failure.submission_id
            if isinstance(failure, RejectedSubmissionFailure)
            else None,
            failure_decision_id=failure.decision_id
            if isinstance(failure, RejectedSubmissionFailure)
            else None,
            failure_review_generation=failure.review_generation
            if isinstance(failure, RejectedSubmissionFailure)
            else None,
            actor_user_id=actor.system_user_id,
            actor_person_id=actor.person_id,
            reason=command.context.reason,
        )
        if (
            isinstance(failure, UnableToCompleteFailure)
            and failure.assignment_generation != command.source_assignment_generation
        ):
            raise _error("failure_basis_stale")
        _replacement_assignments(db, decision=decision)
        policies = {
            source.id: _policy(
                db, enrollment=source, revision_id=command.source_policy_revision_id
            ),
            replacement.id: _policy(
                db,
                enrollment=replacement,
                revision_id=command.replacement_policy_revision_id,
            ),
        }
        coverage = command.coverage
        _replacement_coverage(
            db,
            source=source,
            replacement=replacement,
            decision=decision,
            coverage=coverage,
        )
        _replacement_failure(
            db, decision=decision, source=source, order=orders[source.id]
        )
        _replacement_independence(
            db, actor=actor, enrollments=(source, replacement), policies=policies
        )
    if _staff(db, command) != actor:
        raise _error("denied")
    db.add(decision)
    db.flush()
    if isinstance(
        _replacement_scope(source, replacement), OutageInfrastructureReplacementScope
    ):
        from app.services.field.completion_outage_scope import (
            stage_outage_replacement_proof,
        )

        stage_outage_replacement_proof(
            db,
            decision=decision,
            source=source,
            replacement=replacement,
            previous=previous,
        )
    for mapping in coverage:
        db.add(
            CompletionReplacementCoverage(
                decision_id=decision.id,
                source_criterion_id=mapping.source_criterion_id,
                replacement_criterion_id=mapping.replacement_criterion_id,
            )
        )
    if head is None:
        head = CompletionReplacementHead(
            source_enrollment_id=source.id,
            decision_id=decision.id,
            generation=decision.generation,
        )
        db.add(head)
    head.decision_id = decision.id
    head.generation = decision.generation
    head.replacement_enrollment_id = (
        replacement.id if decision.state == ReplacementState.approved else None
    )
    db.add(
        CompletionReplacementReceipt(
            idempotency_key=key, fingerprint=fingerprint, decision_id=decision.id
        )
    )
    details: dict[str, object] = {
        "action": type(command).__name__,
        "enrollment_id": str(source.id),
        "replacement_enrollment_id": str(replacement.id),
        "replacement_decision_id": str(decision.id),
        "generation": decision.generation,
        "state": decision.state,
        "reason": command.context.reason,
        "command_id": str(command.context.command_id),
        "correlation_id": str(command.context.correlation_id),
    }
    audit_adapter.stage(
        db,
        AuditRecord(
            action="field.completion_review_recorded",
            entity_type="completion_replacement",
            entity_id=str(source.id),
            actor=AuditActor.user(command.context.actor),
            details=details,
            metadata=details,
        ),
    )
    emit_event(
        db,
        EventType.field_completion_review_recorded,
        details,
        actor=command.context.actor,
    )
    db.flush()
    return _replacement_outcome(decision)


def failed_scope_replacement_workspace(
    db: Session, *, query: FailedScopeReplacementWorkspaceQuery
) -> FailedScopeReplacementWorkspace:
    permission = READ if query.purpose == ReplacementWorkspacePurpose.read else WRITE
    actor = revalidate_staff_admission(
        db, admission=query.access, permission=permission, write=permission == WRITE
    )
    source = db.scalar(
        select(CompletionEnrollment).where(
            CompletionEnrollment.public_id == query.work_order_public_id
        )
    )
    if source is None:
        raise _error("enrollment_required")
    enrolled, terminal = _replacement_graph(db, source=source)
    engineering_proof_errors: dict[UUID, DomainError] = {}
    for candidate in enrolled:
        if candidate.id == source.id:
            continue
        try:
            _engineering_replacement_proof(
                db,
                source=source,
                replacement=candidate,
                access=query.access,
                purpose="read" if permission == READ else "write",
            )
        except DomainError as exc:
            engineering_proof_errors[candidate.id] = exc

    orders = {item.id: _historical_replacement_order(db, item) for item in enrolled}
    criteria = {
        item.id: _criteria_head(db, item)
        for item in sorted(enrolled, key=lambda item: item.id)
    }
    for kind, identity in sorted(
        {(item.policy_scope_type, item.policy_scope_id) for item in enrolled}
    ):
        _policy_head(db, kind, identity)
    review_heads = {
        item.id: db.scalar(
            select(CompletionReviewHead)
            .where(CompletionReviewHead.enrollment_id == item.id)
            .with_for_update()
            .execution_options(populate_existing=True)
        )
        for item in sorted(enrolled, key=lambda item: item.id)
    }
    replacement_heads = {
        item.source_enrollment_id: item
        for item in db.scalars(
            select(CompletionReplacementHead)
            .where(
                CompletionReplacementHead.source_enrollment_id.in_(
                    tuple(item.id for item in enrolled)
                )
            )
            .order_by(CompletionReplacementHead.source_enrollment_id)
            .with_for_update()
            .execution_options(populate_existing=True)
        ).all()
    }

    def snapshot(item: CompletionEnrollment) -> ReplacementEnrollmentSnapshot:
        order = orders[item.id]
        assignment = work_order_commands.current_assignment(
            db, query=CurrentWorkOrderAssignmentQuery(order.id)
        )
        policy = _policy_head(db, item.policy_scope_type, item.policy_scope_id)
        head = review_heads[item.id]
        failure: UnableToCompleteFailure | RejectedSubmissionFailure | None = None
        event = db.scalar(
            select(FieldJobEvent)
            .where(FieldJobEvent.work_order_mirror_id == order.id)
            .order_by(FieldJobEvent.received_at.desc(), FieldJobEvent.id.desc())
            .limit(1)
        )
        if (
            _scope_matches(order, item)
            and event is not None
            and event.event == "unable_to_complete"
            and event.new_status == "canceled"
            and order.status == "canceled"
            and assignment is not None
            and assignment.generation is not None
            and event.assignment_generation == assignment.generation
        ):
            failure = UnableToCompleteFailure(event.id, assignment.generation)
        elif head is not None and head.decision_id is not None:
            rejected = db.get(CompletionReviewDecision, head.decision_id)
            submission = db.get(CompletionSubmission, head.submission_id)
            if (
                rejected is not None
                and rejected.state == ReviewState.rejected
                and submission is not None
                and _submission_current(db, item, submission, order)
            ):
                failure = RejectedSubmissionFailure(
                    submission.id, rejected.id, head.generation
                )
        policy_row = (
            db.get(CompletionReviewPolicy, policy.revision_id) if policy else None
        )
        review_state = ReviewState.legacy_not_reviewed
        if head is not None:
            decision_row = (
                db.get(CompletionReviewDecision, head.decision_id)
                if head.decision_id
                else None
            )
            submission_row = db.get(CompletionSubmission, head.submission_id)
            review_state = (
                ReviewState(decision_row.state)
                if decision_row
                else ReviewState.submitted
            )
            if review_state == ReviewState.accepted and (
                submission_row is None
                or not _submission_current(db, item, submission_row, order)
            ):
                review_state = ReviewState.stale
        return ReplacementEnrollmentSnapshot(
            enrollment_id=item.id,
            work_order_public_id=item.public_id,
            title=order.title,
            status=order.status,
            criteria_revision_id=criteria[item.id].revision_id,
            policy_revision_id=policy.revision_id if policy else None,
            assignment_generation=assignment.generation if assignment else None,
            criteria=_issued_criteria(db, enrollment=item),
            review_generation=head.generation if head else 0,
            failure_basis=failure,
            independent_reviewer_required=policy_row.independent_reviewer_required
            if policy_row
            else None,
            current_review_state=review_state,
        )

    snapshots = {item.id: snapshot(item) for item in enrolled}
    by_id = {item.id: item for item in enrolled}
    records = []
    for decision in db.scalars(
        select(CompletionReplacementDecision)
        .where(CompletionReplacementDecision.source_enrollment_id == source.id)
        .order_by(CompletionReplacementDecision.generation)
    ).all():
        replacement = by_id.get(decision.replacement_enrollment_id)
        if replacement is None:
            raise _error("replacement_stale")
        records.append(
            FailedScopeReplacementRecord(
                decision_id=decision.id,
                source_enrollment_id=source.id,
                replacement_enrollment_id=replacement.id,
                replacement_work_order_public_id=replacement.public_id,
                replacement_title=orders[replacement.id].title,
                generation=decision.generation,
                state=ReplacementState(decision.state),
                reason=decision.reason,
                decided_at=decision.decided_at,
                coverage=tuple(
                    CriterionCoverage(
                        item.source_criterion_id, item.replacement_criterion_id
                    )
                    for item in db.scalars(
                        select(CompletionReplacementCoverage)
                        .where(CompletionReplacementCoverage.decision_id == decision.id)
                        .order_by(CompletionReplacementCoverage.source_criterion_id)
                    ).all()
                ),
            )
        )
    head = replacement_heads.get(source.id)
    current_record = next(
        (item for item in records if head and item.decision_id == head.decision_id),
        None,
    )
    blocker = None
    if terminal:
        blocker = "origin_ticket_terminal"
    elif not _scope_matches(orders[source.id], source):
        blocker = "scope_mismatch"
    elif snapshots[source.id].failure_basis is None:
        blocker = "failure_basis_stale"
    elif head is not None and head.replacement_enrollment_id is not None:
        blocker = "replacement_conflict"
    candidates = []
    for item in enrolled:
        if item.id == source.id or not _scope_matches(orders[item.id], item):
            continue
        try:
            _replacement_scope(source, item)
            if item.id in engineering_proof_errors:
                continue
            if any(
                value.replacement_enrollment_id == item.id
                for value in replacement_heads.values()
            ):
                continue
            visited = {source.id}
            cursor = item.id
            while cursor not in visited:
                visited.add(cursor)
                successor = replacement_heads.get(cursor)
                if successor is None or successor.replacement_enrollment_id is None:
                    break
                cursor = successor.replacement_enrollment_id
            else:
                continue
            snapshot_item = snapshots[item.id]
            if (
                snapshot_item.assignment_generation is None
                or snapshot_item.policy_revision_id is None
            ):
                continue
            source_policy = snapshots[source.id].policy_revision_id
            if source_policy is None:
                continue
            _replacement_independence(
                db,
                actor=actor,
                enrollments=(source, item),
                policies={
                    source.id: _policy(
                        db, enrollment=source, revision_id=source_policy
                    ),
                    item.id: _policy(
                        db,
                        enrollment=item,
                        revision_id=snapshot_item.policy_revision_id,
                    ),
                },
            )
        except DomainError:
            continue
        candidates.append(snapshot_item)
    if blocker is None and not candidates:
        blocker = "replacement_candidate_required"
    if (
        revalidate_staff_admission(
            db, admission=query.access, permission=permission, write=permission == WRITE
        )
        != actor
    ):
        raise _error("denied")
    return FailedScopeReplacementWorkspace(
        source=snapshots[source.id],
        expected_generation=head.generation if head else 0,
        current_decision=current_record,
        history=tuple(records),
        candidates=tuple(candidates),
        review_readiness=_readiness(
            "review_failed_scope_replacement", source.public_id, blocker
        ),
        revoke_readiness=_readiness(
            "revoke_failed_scope_replacement",
            source.public_id,
            None
            if head and head.replacement_enrollment_id is not None
            else "replacement_required",
        ),
    )


def _historical_replacement_order(
    db: Session, enrollment: CompletionEnrollment
) -> WorkOrder:
    row = db.scalar(
        select(WorkOrder)
        .where(WorkOrder.id == enrollment.work_order_id)
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    if row is None:
        raise _error("not_found")
    return row


def current_replacement_evidence(
    db: Session, *, query: CurrentReplacementEvidenceQuery
) -> CurrentReplacementEvidence:
    """Fresh immutable proof for accountable follow-up; never a second approval path."""
    actor = revalidate_staff_admission(
        db, admission=query.access, permission=WRITE, write=True
    )
    decision = db.get(
        CompletionReplacementDecision, query.decision_id, populate_existing=True
    )
    if decision is None or decision.state != ReplacementState.approved:
        raise _error("replacement_required")
    source = _enrollment(db, decision.source_enrollment_id)
    replacement = _enrollment(db, decision.replacement_enrollment_id)
    if source.work_order_id != query.source_work_order_id:
        raise _error("scope_mismatch")
    failure = query.failure_basis
    if isinstance(failure, UnableToCompleteFailure):
        exact = (
            decision.failure_event_id == failure.event_id
            and decision.source_assignment_generation == failure.assignment_generation
        )
    else:
        exact = (
            decision.failure_submission_id == failure.submission_id
            and decision.failure_decision_id == failure.decision_id
            and decision.failure_review_generation == failure.review_generation
        )
    if not exact:
        raise _error("failure_basis_stale")
    enrolled, terminal = _replacement_graph(db, source=source)
    if terminal:
        raise _error("origin_ticket_terminal")
    _engineering_replacement_proof(
        db, source=source, replacement=replacement, access=query.access, purpose="write"
    )
    orders = {item.id: _work_order(db, item) for item in enrolled}
    for item in sorted(enrolled, key=lambda item: item.id):
        _criteria_head(db, item)
    for kind, identity in sorted(
        {(item.policy_scope_type, item.policy_scope_id) for item in enrolled}
    ):
        _policy_head(db, kind, identity)
    for item in sorted(enrolled, key=lambda item: item.id):
        db.scalar(
            select(CompletionReviewHead)
            .where(CompletionReviewHead.enrollment_id == item.id)
            .with_for_update()
            .execution_options(populate_existing=True)
        )
    head = db.scalar(
        select(CompletionReplacementHead)
        .where(CompletionReplacementHead.source_enrollment_id == source.id)
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    if (
        head is None
        or head.decision_id != decision.id
        or head.generation != decision.generation
        or head.replacement_enrollment_id != replacement.id
    ):
        raise _error("replacement_stale")
    _replacement_fresh(
        db,
        decision=decision,
        source=source,
        replacement=replacement,
        orders=orders,
        access=query.access,
    )
    if (
        revalidate_staff_admission(
            db, admission=query.access, permission=WRITE, write=True
        )
        != actor
    ):
        raise _error("denied")
    return CurrentReplacementEvidence(
        decision_id=decision.id,
        source_work_order_id=source.work_order_id,
        replacement_work_order_id=replacement.work_order_id,
        source_enrollment_id=source.id,
        replacement_enrollment_id=replacement.id,
        generation=head.generation,
    )
