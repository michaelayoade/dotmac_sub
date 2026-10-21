"""Accountable failed visits, independently of chat, scheduling and customer closure."""

from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from uuid import UUID

from sqlalchemy import exists, or_, select, text
from sqlalchemy.orm import Session

from app.models.field_completion_review import (
    CompletionEnrollment,
    CompletionReviewDecision,
    CompletionSubmission,
)
from app.models.field_job_event import FieldJobEvent
from app.models.field_vendor import FieldVendorUser
from app.models.field_visit_followup import (
    FieldVisitFollowup,
    FieldVisitFollowupReceipt,
    FieldVisitFollowupRevision,
)
from app.models.party import Party
from app.models.service_team import ServiceTeam, ServiceTeamMember
from app.models.system_user import SystemUser, SystemUserType
from app.models.work_order import WorkOrder
from app.services.action_readiness import ActionReadiness, ReadinessState
from app.services.audit_adapter import AuditActor, AuditRecord, audit_adapter
from app.services.domain_errors import DomainError
from app.services.events import EventType, emit_event
from app.services.field.completion_review_contracts import READ as COMPLETION_READ
from app.services.field.completion_review_contracts import WRITE as COMPLETION_WRITE
from app.services.field.completion_review_contracts import CurrentReplacementEvidence
from app.services.field.visit_followup_contracts import (
    CONCERN,
    OWNER,
    READ,
    WRITE,
    ApprovedFollowupReplacementOption,
    ApprovedFollowupReplacementQuery,
    DecideVisitFollowup,
    FailedVisitIntake,
    FailedVisitIntakeQuery,
    FailedVisitSourceQuery,
    FailureSource,
    FailureSourceKind,
    FollowupDisposition,
    FollowupPersonOption,
    FollowupResponsibility,
    FollowupResponsibilityOptions,
    FollowupResponsibilityOptionsQuery,
    FollowupTeamOption,
    OpenVisitFollowup,
    PendingFailureSource,
    VisitFollowupOutcome,
    VisitFollowupQueue,
    VisitFollowupQueueQuery,
    VisitFollowupQueueRow,
    VisitFollowupRecordedEvent,
    VisitFollowupRevisionView,
    VisitFollowupWorkspace,
    VisitFollowupWorkspaceQuery,
)
from app.services.owner_commands import OwnerCommandDefinition, execute_owner_command
from app.services.permission_admission import PermissionAdmission
from app.services.staff_permission_admission import (
    AuthorizedStaffPerson,
    revalidate_staff_admission,
)
from app.services.work_order_assignment_contracts import WorkOrderLinkageFence
from app.services.work_order_commands import lock_work_order_linkage

Command = OpenVisitFollowup | DecideVisitFollowup


@dataclass(frozen=True, slots=True)
class _FailureFact:
    source: FailureSource
    work_order_id: UUID
    occurred_at: datetime
    reason: str
    submission_id: UUID | None = None


def _error(code: str, message: str) -> DomainError:
    return DomainError(code=f"{OWNER}.{code}", message=message, retryable=False)


def _utc(value: datetime) -> datetime:
    return value if value.tzinfo else value.replace(tzinfo=UTC)


def _reader(
    db: Session, access: PermissionAdmission, completion: PermissionAdmission
) -> AuthorizedStaffPerson:
    actor = revalidate_staff_admission(
        db, admission=access, permission=READ, write=False
    )
    other = revalidate_staff_admission(
        db, admission=completion, permission=COMPLETION_READ, write=False
    )
    if actor != other:
        raise _error(
            "denied",
            "Both read admissions must belong to the same current staff member.",
        )
    return actor


def _writer(
    db: Session, *, command: Command, source: FailureSource | None = None
) -> AuthorizedStaffPerson:
    actor = revalidate_staff_admission(
        db, admission=command.access, permission=WRITE, write=True
    )
    if (
        command.context.actor != str(actor.system_user_id)
        or command.context.scope != WRITE
    ):
        raise _error("denied", "Current staff dispatch admission required.")
    if source and source.kind == FailureSourceKind.rejected_decision:
        if command.completion_read_access is None:
            raise _error("denied", "Current completion history admission required.")
        other = revalidate_staff_admission(
            db,
            admission=command.completion_read_access,
            permission=COMPLETION_READ,
            write=False,
        )
        if actor != other:
            raise _error(
                "denied", "Admissions must belong to the same current staff member."
            )
    if (
        isinstance(command, DecideVisitFollowup)
        and command.replacement_decision_id is not None
    ):
        if command.replacement_access is None:
            raise _error("denied", "Current completion replacement admission required.")
        other = revalidate_staff_admission(
            db,
            admission=command.replacement_access,
            permission=COMPLETION_WRITE,
            write=True,
        )
        if actor != other:
            raise _error(
                "denied", "Admissions must belong to the same current staff member."
            )
    return actor


def _failure(db: Session, source: FailureSource) -> _FailureFact:
    if source.kind not in (
        FailureSourceKind.unable_event,
        FailureSourceKind.rejected_decision,
    ):
        raise _error("invalid_request", "Choose an exact supported failure source.")
    if source.kind == FailureSourceKind.unable_event:
        row = db.scalar(
            select(FieldJobEvent)
            .where(
                FieldJobEvent.id == source.source_id,
                FieldJobEvent.event == "unable_to_complete",
            )
            .execution_options(populate_existing=True)
        )
        if row is None:
            raise _error(
                "source_unavailable", "Exact qualifying failure source unavailable."
            )
        reason = (row.payload or {}).get("reason")
        return _FailureFact(
            source,
            row.work_order_mirror_id,
            _utc(row.occurred_at),
            row.note or (reason if isinstance(reason, str) else "Unable to complete"),
        )
    decision = db.scalar(
        select(CompletionReviewDecision)
        .where(
            CompletionReviewDecision.id == source.source_id,
            CompletionReviewDecision.state == "rejected",
        )
        .execution_options(populate_existing=True)
    )
    if decision is None:
        raise _error(
            "source_unavailable", "Exact qualifying failure source unavailable."
        )
    enrollment = db.get(CompletionEnrollment, decision.enrollment_id)
    submission = db.get(CompletionSubmission, decision.submission_id)
    if (
        enrollment is None
        or submission is None
        or submission.enrollment_id != enrollment.id
    ):
        raise _error(
            "source_unavailable", "Exact qualifying failure source unavailable."
        )
    return _FailureFact(
        source,
        enrollment.work_order_id,
        _utc(decision.decided_at),
        decision.reason,
        submission.id,
    )


def _source(row: FieldVisitFollowup) -> FailureSource:
    if row.field_event_id is not None:
        return FailureSource(FailureSourceKind.unable_event, row.field_event_id)
    if row.rejected_decision_id is None:
        raise _error("source_unavailable", "Recorded failure source is inconsistent.")
    return FailureSource(FailureSourceKind.rejected_decision, row.rejected_decision_id)


def _work_order(db: Session, work_order_id: UUID) -> WorkOrder:
    # The canonical linkage fence precedes the WorkOrder row, as in completion enrollment.
    observed = db.scalar(select(WorkOrder).where(WorkOrder.id == work_order_id))
    if observed is None:
        raise _error(
            "source_unavailable", "Exact qualifying failure source unavailable."
        )
    lock_work_order_linkage(
        db,
        query=WorkOrderLinkageFence(
            ticket_id=observed.origin_ticket_id, work_order_id=observed.id
        ),
    )
    row = db.scalar(
        select(WorkOrder)
        .where(WorkOrder.id == work_order_id)
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    if row is None:
        raise _error(
            "source_unavailable", "Exact qualifying failure source unavailable."
        )
    return row


def _responsibility(
    db: Session, value: FollowupResponsibility, failure: _FailureFact
) -> UUID | None:
    if value.due_at.tzinfo is None or _utc(value.due_at) < failure.occurred_at:
        raise _error(
            "invalid_request",
            "Due time must include a timezone and cannot precede the failure.",
        )
    team = db.scalar(
        select(ServiceTeam)
        .where(ServiceTeam.id == value.team_id)
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    if team is None or not team.is_active:
        raise _error(
            "responsibility_unavailable", "Select an active responsible service team."
        )
    if value.system_user_id is None:
        return None
    staff = db.scalar(
        select(SystemUser)
        .where(SystemUser.id == value.system_user_id)
        .execution_options(populate_existing=True)
    )
    if (
        staff is None
        or not staff.is_active
        or staff.user_type != SystemUserType.system_user
        or staff.person_party_id is None
    ):
        raise _error(
            "responsibility_unavailable",
            "Select an active native staff member of the responsible team.",
        )
    person = db.scalar(
        select(Party)
        .where(Party.id == staff.person_party_id)
        .execution_options(populate_existing=True)
    )
    vendor = db.scalar(
        select(FieldVendorUser.id)
        .where(FieldVendorUser.system_user_id == staff.id)
        .limit(1)
    )
    member = db.scalar(
        select(ServiceTeamMember)
        .where(
            ServiceTeamMember.team_id == team.id,
            ServiceTeamMember.person_id == staff.person_party_id,
        )
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    # A member lock may wait while staff identity is changed. Refresh native facts afterward.
    original_person_id = staff.person_party_id
    staff = db.scalar(
        select(SystemUser)
        .where(SystemUser.id == value.system_user_id)
        .execution_options(populate_existing=True)
    )
    person = db.scalar(
        select(Party)
        .where(Party.id == original_person_id)
        .execution_options(populate_existing=True)
    )
    vendor = db.scalar(
        select(FieldVendorUser.id)
        .where(FieldVendorUser.system_user_id == value.system_user_id)
        .limit(1)
    )
    if (
        staff is None
        or not staff.is_active
        or staff.user_type != SystemUserType.system_user
        or staff.person_party_id != original_person_id
        or person is None
        or person.party_type != "person"
        or person.status != "active"
        or vendor is not None
        or member is None
        or not member.is_active
    ):
        raise _error(
            "responsibility_unavailable",
            "Select an active native staff member of the responsible team.",
        )
    return original_person_id


def _fingerprint(command: Command) -> str:
    material = asdict(command)
    material.pop("access")
    material.pop("completion_read_access", None)
    material.pop("replacement_access", None)
    material["context"] = {
        "actor": command.context.actor,
        "scope": command.context.scope,
        "reason": command.context.reason,
    }
    # Equivalent offsets name the same exact due instant.
    material["responsibility"]["due_at"] = command.responsibility.due_at.astimezone(
        UTC
    ).isoformat()
    return hashlib.sha256(
        json.dumps(
            material, sort_keys=True, separators=(",", ":"), default=str
        ).encode()
    ).hexdigest()


def _intent(db: Session, command: Command) -> tuple[UUID, str]:
    try:
        key = UUID(command.context.idempotency_key or "")
    except ValueError as exc:
        raise _error(
            "invalid_request", "A stable UUID intent key is required."
        ) from exc
    if (
        not command.reason.strip()
        or len(command.reason) > 4000
        or command.context.reason != command.reason
        or command.responsibility.due_at.tzinfo is None
    ):
        raise _error(
            "invalid_request",
            "Provide an exact review reason and timezone-aware due time.",
        )
    if db.get_bind().dialect.name == "postgresql":
        db.execute(
            text("SELECT pg_advisory_xact_lock(hashtextextended(:key, 0))"),
            {"key": f"{OWNER}:intent:{key}"},
        )
    return key, _fingerprint(command)


def _outcome(
    db: Session, revision: FieldVisitFollowupRevision, *, replayed: bool
) -> VisitFollowupOutcome:
    obligation = db.get(FieldVisitFollowup, revision.obligation_id)
    if obligation is None:
        raise _error("source_unavailable", "Recorded followup is inconsistent.")
    return VisitFollowupOutcome(
        obligation.id,
        obligation.work_order_id,
        _source(obligation),
        revision.id,
        revision.sequence,
        FollowupDisposition(revision.disposition),
        revision.responsible_team_id,
        revision.responsible_system_user_id,
        revision.responsible_person_id,
        _utc(revision.due_at),
        revision.replacement_decision_id,
        replayed,
    )


def open_followup(db: Session, *, command: OpenVisitFollowup) -> VisitFollowupOutcome:
    return execute_owner_command(
        db,
        definition=OwnerCommandDefinition(OWNER, CONCERN, "open_followup"),
        context=command.context,
        operation=lambda: _stage(db, command=command),
    )


def decide_followup(
    db: Session, *, command: DecideVisitFollowup
) -> VisitFollowupOutcome:
    return execute_owner_command(
        db,
        definition=OwnerCommandDefinition(OWNER, CONCERN, "decide_followup"),
        context=command.context,
        operation=lambda: _stage(db, command=command),
    )


def _stage(db: Session, *, command: Command) -> VisitFollowupOutcome:
    actor = _writer(
        db,
        command=command,
        source=command.source if isinstance(command, OpenVisitFollowup) else None,
    )
    key, fingerprint = _intent(db, command)
    receipt = db.get(FieldVisitFollowupReceipt, key)
    if receipt is not None:
        revision = db.get(FieldVisitFollowupRevision, receipt.revision_id)
        obligation = (
            db.get(FieldVisitFollowup, revision.obligation_id) if revision else None
        )
        if obligation is None or revision is None:
            raise _error(
                "source_unavailable", "Recorded followup receipt is inconsistent."
            )
        if _writer(db, command=command, source=_source(obligation)) != actor:
            raise _error("denied", "Staff identity changed.")
        if receipt.fingerprint != fingerprint:
            raise _error(
                "idempotency_conflict",
                "This intent key was used for different reviewed input.",
            )
        return _outcome(db, revision, replayed=True)
    if isinstance(command, OpenVisitFollowup):
        failure = _failure(db, command.source)
        obligation = None
    else:
        obligation = db.get(FieldVisitFollowup, command.obligation_id)
        if obligation is None:
            raise _error("not_found", "Followup unavailable.")
        _writer(db, command=command, source=_source(obligation))
        failure = _failure(db, _source(obligation))
    # Optional replacement proof is validated by the completion owner before its graph locks.
    _validate_replacement(db, command=command, failure=failure)
    _work_order(db, failure.work_order_id)
    if _failure(db, failure.source) != failure:
        raise _error("source_unavailable", "Exact qualifying failure source changed.")
    if _writer(db, command=command, source=failure.source) != actor:
        raise _error("denied", "Staff identity changed while waiting for the job.")
    if isinstance(command, OpenVisitFollowup):
        selector = (
            FieldVisitFollowup.field_event_id
            if failure.source.kind == FailureSourceKind.unable_event
            else FieldVisitFollowup.rejected_decision_id
        )
        if (
            db.scalar(
                select(FieldVisitFollowup.id).where(
                    selector == failure.source.source_id
                )
            )
            is not None
        ):
            raise _error(
                "already_accounted",
                "This exact failure already has an accountable followup. Open its history.",
            )
        obligation = FieldVisitFollowup(
            work_order_id=failure.work_order_id,
            field_event_id=failure.source.source_id
            if failure.source.kind == FailureSourceKind.unable_event
            else None,
            rejected_decision_id=failure.source.source_id
            if failure.source.kind == FailureSourceKind.rejected_decision
            else None,
            current_sequence=1,
        )
        db.add(obligation)
        db.flush()
        disposition = FollowupDisposition.triage
    else:
        obligation = db.scalar(
            select(FieldVisitFollowup)
            .where(FieldVisitFollowup.id == command.obligation_id)
            .with_for_update()
            .execution_options(populate_existing=True)
        )
        if obligation is None:
            raise _error("not_found", "Followup unavailable.")
        current = db.scalar(
            select(FieldVisitFollowupRevision).where(
                FieldVisitFollowupRevision.obligation_id == obligation.id,
                FieldVisitFollowupRevision.sequence == obligation.current_sequence,
            )
        )
        if current is None or current.id != command.expected_revision_id:
            raise _error(
                "stale_revision",
                "Followup changed. Reload current evidence before a new intent.",
            )
        obligation.current_sequence += 1
        disposition = command.disposition
    person_id = _responsibility(db, command.responsibility, failure)
    if _writer(db, command=command, source=failure.source) != actor:
        raise _error(
            "denied", "Staff identity changed while waiting for responsibility."
        )
    revision = FieldVisitFollowupRevision(
        obligation_id=obligation.id,
        sequence=obligation.current_sequence,
        disposition=disposition.value,
        responsible_team_id=command.responsibility.team_id,
        responsible_system_user_id=command.responsibility.system_user_id,
        responsible_person_id=person_id,
        due_at=command.responsibility.due_at,
        reason=command.reason,
        replacement_decision_id=command.replacement_decision_id
        if isinstance(command, DecideVisitFollowup)
        else None,
        actor_user_id=actor.system_user_id,
        actor_person_id=actor.person_id,
    )
    db.add(revision)
    db.flush()
    db.add(
        FieldVisitFollowupReceipt(
            intent_key=key, fingerprint=fingerprint, revision_id=revision.id
        )
    )
    event = VisitFollowupRecordedEvent(
        obligation.id,
        revision.id,
        obligation.work_order_id,
        revision.sequence,
        disposition,
        failure.source,
    )
    audit_adapter.stage(
        db,
        AuditRecord(
            action="field_visit_followup.recorded",
            entity_type="field_visit_followup",
            entity_id=str(obligation.id),
            actor=AuditActor.user(str(actor.system_user_id), party_id=actor.person_id),
            details={**event.payload(), "reason": command.reason},
        ),
    )
    emit_event(
        db,
        EventType.custom,
        event.payload(),
        actor=str(actor.system_user_id),
        record_only=True,
    )
    db.flush()
    return _outcome(db, revision, replayed=False)


def _validate_replacement(
    db: Session, *, command: Command, failure: _FailureFact
) -> None:
    if (
        not isinstance(command, DecideVisitFollowup)
        or command.replacement_decision_id is None
    ):
        return
    if (
        command.disposition != FollowupDisposition.revisit_required
        or command.replacement_access is None
    ):
        raise _error(
            "invalid_replacement",
            "Approved replacement evidence is valid only for revisit required.",
        )
    if (
        command.replacement_access.principal_id != command.access.principal_id
        or command.replacement_access.principal_type != command.access.principal_type
    ):
        raise _error(
            "denied",
            "Replacement admission must belong to the same current staff member.",
        )
    evidence = _replacement_evidence(
        db,
        access=command.replacement_access,
        decision_id=command.replacement_decision_id,
        failure=failure,
    )
    if command.expected_replacement_generation != evidence.generation:
        raise _error(
            "invalid_replacement",
            "Approved replacement changed. Reload current evidence.",
        )


def _replacement_evidence(
    db: Session,
    *,
    access: PermissionAdmission,
    decision_id: UUID,
    failure: _FailureFact,
) -> CurrentReplacementEvidence:
    from app.models.field_completion_review import CompletionReviewHead
    from app.services.field.completion_review import current_replacement_evidence
    from app.services.field.completion_review_contracts import (
        CurrentReplacementEvidenceQuery,
        RejectedSubmissionFailure,
        UnableToCompleteFailure,
    )

    basis: UnableToCompleteFailure | RejectedSubmissionFailure
    if failure.source.kind == FailureSourceKind.unable_event:
        event = db.get(FieldJobEvent, failure.source.source_id)
        if event is None or event.assignment_generation is None:
            raise _error(
                "invalid_replacement",
                "Exact failed visit assignment evidence unavailable.",
            )
        basis = UnableToCompleteFailure(
            event_id=event.id, assignment_generation=event.assignment_generation
        )
    else:
        decision = db.get(CompletionReviewDecision, failure.source.source_id)
        head = (
            db.get(CompletionReviewHead, decision.enrollment_id) if decision else None
        )
        if decision is None or head is None or head.decision_id != decision.id:
            raise _error(
                "invalid_replacement",
                "Exact current rejected review evidence unavailable.",
            )
        basis = RejectedSubmissionFailure(
            submission_id=decision.submission_id,
            decision_id=decision.id,
            review_generation=head.generation,
        )
    return current_replacement_evidence(
        db,
        query=CurrentReplacementEvidenceQuery(
            access=access,
            source_work_order_id=failure.work_order_id,
            decision_id=decision_id,
            failure_basis=basis,
        ),
    )


def _readiness(
    db: Session, actor: AuthorizedStaffPerson, *, subject: str, permission: str = WRITE
) -> ActionReadiness:
    from app.models.rbac import Role, SystemUserRole
    from app.services.action_readiness import ActionableBlocker, BlockerEvidence
    from app.services.erp_staff_access import active_leave_restriction
    from app.services.permission_authorization import (
        PermissionPrincipal,
        PermissionPrincipalKind,
        has_permission,
    )

    roles = db.scalars(
        select(Role.name)
        .join(SystemUserRole, SystemUserRole.role_id == Role.id)
        .where(
            SystemUserRole.system_user_id == actor.system_user_id,
            Role.is_active.is_(True),
        )
    ).all()
    principal = PermissionPrincipal(
        actor.system_user_id,
        PermissionPrincipalKind.system_user,
        frozenset(roles),
        frozenset(),
    )
    allowed = (
        has_permission(principal, db, permission)
        and active_leave_restriction(db, system_user_id=actor.system_user_id) is None
    )
    message = f"Current {permission} permission without active leave is required."
    return ActionReadiness(
        action_key="field_visit_followup",
        subject_type="failed_visit",
        subject_id=subject,
        owner=OWNER,
        state=ReadinessState.ready if allowed else ReadinessState.blocked,
        evaluated_at=datetime.now(UTC),
        blockers=()
        if allowed
        else (
            ActionableBlocker(
                code=f"{OWNER}.denied",
                owner=OWNER,
                customer_message=message,
                staff_detail=message,
                evidence=BlockerEvidence(summary=message),
            ),
        ),
    )


def pending_failures(
    db: Session, *, query: FailedVisitIntakeQuery
) -> FailedVisitIntake:
    actor = _reader(db, query.access, query.completion_access)
    if not 1 <= query.limit <= 100:
        raise _error("invalid_request", "Page size must be between 1 and 100.")
    unable_pending = exists(
        select(FieldJobEvent.id).where(
            FieldJobEvent.work_order_mirror_id == WorkOrder.id,
            FieldJobEvent.event == "unable_to_complete",
            ~exists(
                select(FieldVisitFollowup.id).where(
                    FieldVisitFollowup.field_event_id == FieldJobEvent.id
                )
            ),
        )
    )
    rejected_pending = exists(
        select(CompletionReviewDecision.id)
        .join(
            CompletionEnrollment,
            CompletionEnrollment.id == CompletionReviewDecision.enrollment_id,
        )
        .where(
            CompletionEnrollment.work_order_id == WorkOrder.id,
            CompletionReviewDecision.state == "rejected",
            ~exists(
                select(FieldVisitFollowup.id).where(
                    FieldVisitFollowup.rejected_decision_id
                    == CompletionReviewDecision.id
                )
            ),
        )
    )
    statement = (
        select(WorkOrder)
        .where(or_(unable_pending, rejected_pending))
        .order_by(WorkOrder.id)
        .limit(query.limit + 1)
    )
    if query.after_work_order_id is not None:
        statement = statement.where(WorkOrder.id > query.after_work_order_id)
    jobs = db.scalars(statement).all()
    rows = []
    for job in jobs[: query.limit]:
        sources = [
            FailureSource(FailureSourceKind.unable_event, source_id)
            for source_id in db.scalars(
                select(FieldJobEvent.id).where(
                    FieldJobEvent.work_order_mirror_id == job.id,
                    FieldJobEvent.event == "unable_to_complete",
                    ~exists(
                        select(FieldVisitFollowup.id).where(
                            FieldVisitFollowup.field_event_id == FieldJobEvent.id
                        )
                    ),
                )
            ).all()
        ]
        sources.extend(
            FailureSource(FailureSourceKind.rejected_decision, source_id)
            for source_id in db.scalars(
                select(CompletionReviewDecision.id)
                .join(
                    CompletionEnrollment,
                    CompletionEnrollment.id == CompletionReviewDecision.enrollment_id,
                )
                .where(
                    CompletionEnrollment.work_order_id == job.id,
                    CompletionReviewDecision.state == "rejected",
                    ~exists(
                        select(FieldVisitFollowup.id).where(
                            FieldVisitFollowup.rejected_decision_id
                            == CompletionReviewDecision.id
                        )
                    ),
                )
            ).all()
        )
        for source in sources:
            failure = _failure(db, source)
            rows.append(
                PendingFailureSource(
                    source,
                    job.id,
                    job.public_id,
                    job.title or str(job.public_id),
                    failure.occurred_at,
                    failure.reason,
                    _readiness(db, actor, subject=str(source.source_id)),
                )
            )
    return FailedVisitIntake(
        tuple(rows), jobs[query.limit - 1].id if len(jobs) > query.limit else None
    )


def workspace(
    db: Session, *, query: VisitFollowupWorkspaceQuery
) -> VisitFollowupWorkspace:
    actor = _reader(db, query.access, query.completion_access)
    obligation = db.get(FieldVisitFollowup, query.obligation_id)
    if obligation is None:
        raise _error("not_found", "Followup unavailable.")
    job = db.get(WorkOrder, obligation.work_order_id)
    if job is None:
        raise _error("not_found", "Failed work order unavailable.")
    revisions = db.scalars(
        select(FieldVisitFollowupRevision)
        .where(FieldVisitFollowupRevision.obligation_id == obligation.id)
        .order_by(FieldVisitFollowupRevision.sequence.desc())
    ).all()
    views = []
    for row in revisions:
        team = db.get(ServiceTeam, row.responsible_team_id)
        person = (
            db.get(Party, row.responsible_person_id)
            if row.responsible_person_id
            else None
        )
        views.append(
            VisitFollowupRevisionView(
                row.id,
                row.sequence,
                FollowupDisposition(row.disposition),
                row.responsible_team_id,
                team.name if team else "Historical team unavailable",
                row.responsible_system_user_id,
                row.responsible_person_id,
                person.display_name if person else None,
                _utc(row.due_at),
                row.reason,
                row.replacement_decision_id,
                _utc(row.decided_at),
            )
        )
    return VisitFollowupWorkspace(
        obligation.id,
        obligation.work_order_id,
        job.public_id,
        _source(obligation),
        tuple(views),
        _readiness(db, actor, subject=str(obligation.id)),
        _readiness(db, actor, subject=str(obligation.id), permission=COMPLETION_WRITE),
    )


def responsibility_options(
    db: Session, *, query: FollowupResponsibilityOptionsQuery
) -> FollowupResponsibilityOptions:
    _reader(db, query.access, query.completion_access)
    teams = db.scalars(
        select(ServiceTeam)
        .where(ServiceTeam.is_active.is_(True))
        .order_by(ServiceTeam.name)
        .limit(200)
    ).all()
    people = []
    if query.team_id is not None:
        candidates = db.execute(
            select(SystemUser.id, Party.id, Party.display_name)
            .join(Party, Party.id == SystemUser.person_party_id)
            .join(ServiceTeamMember, ServiceTeamMember.person_id == Party.id)
            .join(ServiceTeam, ServiceTeam.id == ServiceTeamMember.team_id)
            .where(
                ServiceTeam.id == query.team_id,
                ServiceTeam.is_active.is_(True),
                ServiceTeamMember.is_active.is_(True),
                SystemUser.is_active.is_(True),
                SystemUser.user_type == SystemUserType.system_user,
                Party.party_type == "person",
                Party.status == "active",
                ~exists(
                    select(FieldVendorUser.id).where(
                        FieldVendorUser.system_user_id == SystemUser.id
                    )
                ),
            )
            .order_by(Party.display_name, SystemUser.id)
            .limit(200)
        ).all()
        people = [
            FollowupPersonOption(user_id, person_id, label or "Staff member")
            for user_id, person_id, label in candidates
        ]
    return FollowupResponsibilityOptions(
        tuple(FollowupTeamOption(team.id, team.name) for team in teams), tuple(people)
    )


def followup_queue(
    db: Session, *, query: VisitFollowupQueueQuery
) -> VisitFollowupQueue:
    _reader(db, query.access, query.completion_access)
    if not 1 <= query.limit <= 100:
        raise _error("invalid_request", "Page size must be between 1 and 100.")
    statement = (
        select(
            FieldVisitFollowup,
            FieldVisitFollowupRevision,
            WorkOrder,
            ServiceTeam,
            Party,
        )
        .join(
            FieldVisitFollowupRevision,
            (FieldVisitFollowupRevision.obligation_id == FieldVisitFollowup.id)
            & (
                FieldVisitFollowupRevision.sequence
                == FieldVisitFollowup.current_sequence
            ),
        )
        .join(WorkOrder, WorkOrder.id == FieldVisitFollowup.work_order_id)
        .join(
            ServiceTeam,
            ServiceTeam.id == FieldVisitFollowupRevision.responsible_team_id,
        )
        .outerjoin(Party, Party.id == FieldVisitFollowupRevision.responsible_person_id)
        .order_by(FieldVisitFollowup.id)
        .limit(query.limit + 1)
    )
    if not query.include_closed:
        statement = statement.where(
            FieldVisitFollowupRevision.disposition
            != FollowupDisposition.no_revisit.value
        )
    if query.after_obligation_id:
        statement = statement.where(FieldVisitFollowup.id > query.after_obligation_id)
    records = db.execute(statement).all()
    return VisitFollowupQueue(
        tuple(
            VisitFollowupQueueRow(
                obligation.id,
                job.id,
                job.title or str(job.public_id),
                FollowupDisposition(revision.disposition),
                team.name,
                person.display_name if person else None,
                _utc(revision.due_at),
                revision.replacement_decision_id,
            )
            for obligation, revision, job, team, person in records[: query.limit]
        ),
        records[query.limit - 1][0].id if len(records) > query.limit else None,
    )


def approved_replacement_option(
    db: Session, *, query: ApprovedFollowupReplacementQuery
) -> ApprovedFollowupReplacementOption | None:
    actor = _reader(db, query.access, query.completion_access)
    other = revalidate_staff_admission(
        db, admission=query.replacement_access, permission=COMPLETION_WRITE, write=True
    )
    if actor != other:
        raise _error(
            "denied", "Admissions must belong to the same current staff member."
        )
    obligation = db.get(FieldVisitFollowup, query.obligation_id)
    if obligation is None:
        raise _error("not_found", "Followup unavailable.")
    job = db.get(WorkOrder, obligation.work_order_id)
    if job is None:
        raise _error("not_found", "Failed work order unavailable.")
    failure = _failure(db, _source(obligation))
    from app.services.field.completion_review import failed_scope_replacement_workspace
    from app.services.field.completion_review_contracts import (
        FailedScopeReplacementWorkspaceQuery,
        ReplacementState,
        ReplacementWorkspacePurpose,
    )

    review = failed_scope_replacement_workspace(
        db,
        query=FailedScopeReplacementWorkspaceQuery(
            access=query.replacement_access,
            work_order_public_id=job.public_id,
            purpose=ReplacementWorkspacePurpose.review,
        ),
    )
    current = review.current_decision
    if current is None or current.state != ReplacementState.approved:
        return None
    evidence = _replacement_evidence(
        db,
        access=query.replacement_access,
        decision_id=current.decision_id,
        failure=failure,
    )
    if evidence.generation != current.generation:
        raise _error(
            "invalid_replacement",
            "Approved replacement changed. Reload current evidence.",
        )
    if _reader(db, query.access, query.completion_access) != actor:
        raise _error(
            "denied", "Staff identity changed while reading replacement evidence."
        )
    return ApprovedFollowupReplacementOption(
        current.decision_id,
        current.generation,
        current.replacement_work_order_public_id,
        current.replacement_title,
    )


def pending_source(
    db: Session, *, query: FailedVisitSourceQuery
) -> PendingFailureSource:
    actor = _reader(db, query.access, query.completion_access)
    failure = _failure(db, query.source)
    selector = (
        FieldVisitFollowup.field_event_id
        if query.source.kind == FailureSourceKind.unable_event
        else FieldVisitFollowup.rejected_decision_id
    )
    if (
        db.scalar(
            select(FieldVisitFollowup.id).where(selector == query.source.source_id)
        )
        is not None
    ):
        raise _error(
            "already_accounted",
            "This exact failure already has an accountable followup. Open its history.",
        )
    job = db.get(WorkOrder, failure.work_order_id)
    if job is None:
        raise _error("not_found", "Failed work order unavailable.")
    return PendingFailureSource(
        query.source,
        job.id,
        job.public_id,
        job.title or job.public_id,
        failure.occurred_at,
        failure.reason,
        _readiness(db, actor, subject=str(query.source.source_id)),
    )
