"""Field completion applies fresh accepted evidence through the project owner."""

import hashlib
import json
from dataclasses import asdict
from datetime import UTC, datetime
from uuid import UUID, uuid4

from sqlalchemy import select, text
from sqlalchemy.orm import Session

from app.models.project_field_completion import (
    ProjectFieldCompletionApplication,
    ProjectFieldCompletionBasis,
    ProjectFieldCompletionLineage,
    ProjectFieldCompletionReceipt,
)
from app.models.support import Ticket
from app.models.work_order import WorkOrder
from app.services import projects
from app.services.action_readiness import (
    ActionableBlocker,
    ActionReadiness,
    BlockerEvidence,
    ReadinessState,
)
from app.services.audit_adapter import AuditActor, AuditRecord, audit_adapter
from app.services.domain_errors import DomainError
from app.services.field import completion_review
from app.services.field.completion_project_scope_contracts import (
    CONCERN,
    SCOPE,
    AcceptedProjectCompletionOutcome,
    AcceptedProjectCompletionPreview,
    AcceptedProjectCompletionPreviewQuery,
    ApplyAcceptedProjectCompletion,
    ProjectFieldWorkOrderLabel,
)
from app.services.field.completion_review_contracts import (
    OWNER,
    TicketCompletionBasisEntry,
)
from app.services.owner_commands import OwnerCommandDefinition, execute_owner_command
from app.services.permission_admission import PermissionAdmission
from app.services.project_field_completion_contracts import (
    CompleteAcceptedProjectFieldScope,
    ProjectFieldScope,
    ProjectFieldScopeQuery,
)
from app.services.staff_permission_admission import (
    AuthorizedStaffPerson,
    revalidate_staff_admission,
)
from app.services.work_order_assignment_contracts import WorkOrderLinkageFence
from app.services.work_order_commands import lock_work_order_linkage


def _error(code: str, message: str) -> DomainError:
    return DomainError(code=f"{OWNER}.{code}", message=message)


def _staff(
    db: Session, *, access: PermissionAdmission, task_id: UUID | None
) -> AuthorizedStaffPerson:
    return revalidate_staff_admission(
        db,
        admission=access,
        permission="project:task:write" if task_id else "project:update",
        write=True,
    )


def _scope_locked(
    db: Session, *, project_id: UUID, task_id: UUID | None
) -> ProjectFieldScope:
    observed = projects.project_field_completion_scope(
        db, query=ProjectFieldScopeQuery(project_id=project_id, task_id=task_id)
    )
    required_ids = tuple(item.work_order_id for item in observed.requirements)
    from app.services.topology.outage_completion import lock_completion_scope_graph
    from app.services.topology.outage_completion_contracts import (
        CompletionScopeGraphLocksQuery,
    )

    lock_completion_scope_graph(
        db, query=CompletionScopeGraphLocksQuery(required_work_order_ids=required_ids)
    )
    orders = tuple(
        db.scalars(
            select(WorkOrder)
            .where(WorkOrder.id.in_(required_ids))
            .order_by(WorkOrder.id)
        ).all()
    )
    if {item.id for item in orders} != set(required_ids):
        raise _error("scope_mismatch", "A registered field work order is missing")
    ticket_ids = sorted(
        {item.origin_ticket_id for item in orders if item.origin_ticket_id is not None}
    )
    for ticket_id in ticket_ids:
        lock_work_order_linkage(db, query=WorkOrderLinkageFence(ticket_id=ticket_id))
    for work_order_id in sorted(required_ids):
        lock_work_order_linkage(
            db, query=WorkOrderLinkageFence(ticket_id=None, work_order_id=work_order_id)
        )
    for ticket_id in ticket_ids:
        db.scalar(
            select(Ticket)
            .where(Ticket.id == ticket_id)
            .with_for_update()
            .execution_options(populate_existing=True)
        )
    current = projects.project_field_completion_scope(
        db,
        query=ProjectFieldScopeQuery(project_id=project_id, task_id=task_id, lock=True),
    )
    # Never acquire a newly discovered WO fence while holding the Project lock.
    if (
        current.requirements != observed.requirements
        or current.task_ids != observed.task_ids
    ):
        raise _error(
            "stale_state",
            "Required field scope changed while acquiring locks; refresh and retry",
        )
    return current


def _check_scope(scope: ProjectFieldScope) -> None:
    if not scope.requirements:
        raise _error(
            "enrollment_required",
            "This scope has no explicit enrolled field requirements; use its ordinary lifecycle action",
        )
    if scope.status in {"completed", "canceled", "done"}:
        raise _error("invalid_transition", "Project/task is already terminal")


def _preview_readiness(
    scope: ProjectFieldScope, error: DomainError | None
) -> ActionReadiness:
    blockers = (
        ()
        if error is None
        else (
            ActionableBlocker(
                code=error.code,
                owner="operations.project_lifecycle"
                if error.code.startswith("operations.project_lifecycle.")
                else OWNER,
                customer_message="Field completion is awaiting accepted evidence",
                staff_detail=error.message,
                evidence=BlockerEvidence(summary=error.message),
            ),
        )
    )
    return ActionReadiness(
        action_key="apply_accepted_project_completion",
        subject_type="project_task" if scope.task_id else "project",
        subject_id=str(scope.task_id or scope.project_id),
        owner=OWNER,
        state=ReadinessState.blocked if error else ReadinessState.ready,
        evaluated_at=datetime.now(UTC),
        blockers=blockers,
    )


def preview_accepted_project_completion(
    db: Session, *, query: AcceptedProjectCompletionPreviewQuery
) -> AcceptedProjectCompletionPreview:
    from app.services.field.completion_review_contracts import (
        CompletionBasisParticipant,
        ScopedCompletionBasisPreviewQuery,
    )

    actor = _staff(db, access=query.access, task_id=query.task_id)
    scope = _scope_locked(db, project_id=query.project_id, task_id=query.task_id)
    error = None
    try:
        _check_scope(scope)
        completion_review.preview_scoped_completion_basis(
            db,
            query=ScopedCompletionBasisPreviewQuery(
                required_work_order_ids=tuple(
                    item.work_order_id for item in scope.requirements
                ),
                expected_enrollment_ids=tuple(
                    item.enrollment_id for item in scope.requirements
                ),
                participant=CompletionBasisParticipant.project_task_completion
                if query.task_id
                else CompletionBasisParticipant.project_completion,
                access=query.access,
            ),
        )
        projects.validate_project_field_completion_ready(
            db,
            query=ProjectFieldScopeQuery(
                project_id=scope.project_id, task_id=scope.task_id
            ),
        )
    except DomainError as exc:
        error = exc
    if _staff(db, access=query.access, task_id=query.task_id) != actor:
        raise _error("denied", "Current staff admission changed")
    return AcceptedProjectCompletionPreview(
        scope.project_id,
        scope.task_id,
        scope.title,
        scope.status,
        scope.project_revision,
        scope.task_revision,
        scope.requirement_token,
        scope.requirements,
        _preview_readiness(scope, error),
        tuple(
            ProjectFieldWorkOrderLabel(row.public_id, row.title, row.status)
            for row in db.scalars(
                select(WorkOrder)
                .where(
                    WorkOrder.id.in_(
                        tuple(item.work_order_id for item in scope.requirements)
                    )
                )
                .order_by(WorkOrder.public_id)
            ).all()
        ),
    )


def apply_accepted_project_completion(
    db: Session, *, command: ApplyAcceptedProjectCompletion
) -> AcceptedProjectCompletionOutcome:
    return execute_owner_command(
        db,
        definition=OwnerCommandDefinition(
            owner=OWNER, concern=CONCERN, name=type(command).__name__
        ),
        context=command.context,
        operation=lambda: _stage(db, command=command),
    )


def _stage(
    db: Session, *, command: ApplyAcceptedProjectCompletion
) -> AcceptedProjectCompletionOutcome:
    from app.services.field.completion_review_contracts import (
        CompletionBasisParticipant,
        ScopedCompletionBasisQuery,
    )

    actor = _staff(db, access=command.access, task_id=command.task_id)
    context = command.context
    if (
        context.actor != str(actor.system_user_id)
        or context.scope != SCOPE
        or not context.reason
        or not context.reason.strip()
    ):
        raise _error(
            "invalid_request", "Exact actor, action scope and review reason required"
        )
    try:
        intent_id = UUID(context.idempotency_key or "")
    except ValueError as exc:
        raise _error("invalid_request", "A UUID command intent is required") from exc
    intent = asdict(command)
    intent.pop("access")
    intent["context"] = {
        "actor": context.actor,
        "scope": context.scope,
        "reason": context.reason,
    }
    fingerprint = hashlib.sha256(
        json.dumps(intent, sort_keys=True, default=str).encode()
    ).hexdigest()
    if db.get_bind().dialect.name == "postgresql":
        db.execute(
            text("SELECT pg_advisory_xact_lock(:key)"),
            {
                "key": int.from_bytes(
                    hashlib.sha256(
                        (OWNER + ":project-completion:" + str(intent_id)).encode()
                    ).digest()[:8],
                    "big",
                    signed=True,
                )
            },
        )
    receipt = db.get(ProjectFieldCompletionReceipt, intent_id, populate_existing=True)
    if receipt:
        if receipt.fingerprint != fingerprint:
            raise _error(
                "idempotency_conflict",
                "Command intent was already used with different evidence or scope",
            )
        application = db.get(ProjectFieldCompletionApplication, receipt.application_id)
        if (
            application is None
            or application.actor_user_id != actor.system_user_id
            or _staff(db, access=command.access, task_id=command.task_id) != actor
        ):
            raise _error(
                "denied", "Current staff admission required for historical replay"
            )
        current = _scope_locked(
            db, project_id=command.project_id, task_id=command.task_id
        )
        if (
            current.requirement_token != application.requirement_token
            or application.actor_person_id != actor.person_id
        ):
            raise _error(
                "stale_state", "Historical completion scope or staff identity changed"
            )
        if current.status != ("done" if application.task_id else "completed"):
            raise _error(
                "stale_state",
                "Historical applied completion no longer matches the lifecycle state",
            )
        fresh = completion_review.stage_scoped_completion_basis(
            db,
            query=ScopedCompletionBasisQuery(
                required_work_order_ids=tuple(
                    item.work_order_id for item in current.requirements
                ),
                expected_enrollment_ids=tuple(
                    item.enrollment_id for item in current.requirements
                ),
                participant=CompletionBasisParticipant.project_completion,
            ),
        )
        _compare_applied_basis(db, application_id=application.id, entries=fresh.entries)
        if _staff(db, access=command.access, task_id=command.task_id) != actor:
            raise _error(
                "denied", "Current staff admission changed after replay evidence locks"
            )
        return AcceptedProjectCompletionOutcome(
            application.id,
            application.project_id,
            application.task_id,
            "done" if application.task_id else "completed",
            True,
        )
    scope = _scope_locked(db, project_id=command.project_id, task_id=command.task_id)
    _check_scope(scope)

    def instant(value: datetime | None) -> datetime | None:
        return (
            value.replace(tzinfo=UTC)
            if value is not None and value.tzinfo is None
            else value
        )

    if (
        instant(scope.project_revision),
        instant(scope.task_revision),
        scope.requirement_token,
    ) != (
        instant(command.expected_project_revision),
        instant(command.expected_task_revision),
        command.expected_requirement_token,
    ):
        raise _error(
            "stale_state", "Project, task or required field scope changed; refresh"
        )
    basis = completion_review.stage_scoped_completion_basis(
        db,
        query=ScopedCompletionBasisQuery(
            required_work_order_ids=tuple(
                item.work_order_id for item in scope.requirements
            ),
            expected_enrollment_ids=tuple(
                item.enrollment_id for item in scope.requirements
            ),
            participant=CompletionBasisParticipant.project_completion,
        ),
    )
    if {item.enrollment_id for item in basis.entries} != {
        item.enrollment_id for item in scope.requirements
    }:
        raise _error(
            "scope_mismatch",
            "Accepted basis must preserve every original required enrollment",
        )
    if _staff(db, access=command.access, task_id=command.task_id) != actor:
        raise _error("denied", "Current staff admission changed after evidence locks")
    application = ProjectFieldCompletionApplication(
        id=uuid4(),
        project_id=scope.project_id,
        task_id=scope.task_id,
        expected_project_revision=scope.project_revision,
        expected_task_revision=scope.task_revision,
        requirement_token=scope.requirement_token,
        actor_user_id=actor.system_user_id,
        actor_person_id=actor.person_id,
        intent_id=intent_id,
        command_id=context.command_id,
        reason=context.reason,
    )
    db.add(application)
    db.flush()
    for entry in basis.entries:
        db.add(
            ProjectFieldCompletionBasis(
                application_id=application.id,
                enrollment_id=entry.enrollment_id,
                work_order_id=entry.work_order_id,
                effective_enrollment_id=entry.effective_enrollment_id
                or entry.enrollment_id,
                effective_work_order_id=entry.effective_work_order_id
                or entry.work_order_id,
                criteria_revision_id=entry.criteria_revision_id,
                policy_revision_id=entry.policy_revision_id,
                submission_id=entry.submission_id,
                accepted_decision_id=entry.accepted_decision_id,
                review_generation=entry.review_generation,
                completed_event_id=entry.completed_event_id,
                assignment_generation=entry.assignment_generation,
            )
        )
        for position, decision_id in enumerate(entry.replacement_decision_ids):
            db.add(
                ProjectFieldCompletionLineage(
                    application_id=application.id,
                    enrollment_id=entry.enrollment_id,
                    position=position,
                    decision_id=decision_id,
                )
            )
    db.flush()
    db.add(
        ProjectFieldCompletionReceipt(
            intent_id=intent_id, fingerprint=fingerprint, application_id=application.id
        )
    )
    db.flush()
    db.info["new_project_field_completion_applications"] = (application,)
    try:
        projects.stage_complete_from_accepted_field_scope(
            db,
            command=CompleteAcceptedProjectFieldScope(
                context=context,
                application_id=application.id,
                project_id=scope.project_id,
                task_id=scope.task_id,
                expected_project_revision=scope.project_revision,
                expected_task_revision=scope.task_revision,
                expected_requirement_token=scope.requirement_token,
            ),
        )
    finally:
        db.info.pop("new_project_field_completion_applications", None)
    audit_adapter.stage(
        db,
        AuditRecord(
            action="project.accepted_field_completion_applied",
            entity_type="project_task" if scope.task_id else "project",
            entity_id=str(scope.task_id or scope.project_id),
            actor=AuditActor.user(str(actor.system_user_id), party_id=actor.person_id),
            request_id=str(context.correlation_id),
            metadata={
                "application_id": str(application.id),
                "required_enrollments": [
                    str(item.enrollment_id) for item in basis.entries
                ],
            },
        ),
    )
    return AcceptedProjectCompletionOutcome(
        application.id,
        scope.project_id,
        scope.task_id,
        "done" if scope.task_id else "completed",
    )


def _compare_applied_basis(
    db: Session,
    *,
    application_id: UUID,
    entries: tuple[TicketCompletionBasisEntry, ...],
) -> None:
    persisted = {
        row.enrollment_id: row
        for row in db.scalars(
            select(ProjectFieldCompletionBasis).where(
                ProjectFieldCompletionBasis.application_id == application_id
            )
        ).all()
    }
    if set(persisted) != {entry.enrollment_id for entry in entries}:
        raise _error(
            "stale_state",
            "Current accepted basis no longer matches historical completion",
        )
    for entry in entries:
        row = persisted[entry.enrollment_id]
        names = (
            "work_order_id",
            "criteria_revision_id",
            "policy_revision_id",
            "submission_id",
            "accepted_decision_id",
            "review_generation",
            "completed_event_id",
            "assignment_generation",
        )
        lineage = tuple(
            db.scalars(
                select(ProjectFieldCompletionLineage.decision_id)
                .where(
                    ProjectFieldCompletionLineage.application_id == application_id,
                    ProjectFieldCompletionLineage.enrollment_id == entry.enrollment_id,
                )
                .order_by(ProjectFieldCompletionLineage.position)
            ).all()
        )
        if (
            any(getattr(row, name) != getattr(entry, name) for name in names)
            or row.effective_enrollment_id
            != (entry.effective_enrollment_id or entry.enrollment_id)
            or row.effective_work_order_id
            != (entry.effective_work_order_id or entry.work_order_id)
            or lineage != entry.replacement_decision_ids
        ):
            raise _error(
                "stale_state",
                "Current accepted basis no longer matches historical completion",
            )
