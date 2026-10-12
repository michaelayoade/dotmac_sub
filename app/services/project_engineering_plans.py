"""Native project engineering approval authority; never issues field work."""

import hashlib
import json
from dataclasses import asdict
from datetime import UTC, datetime
from uuid import UUID, uuid4

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.models.network_monitoring import PopSite
from app.models.party import Party
from app.models.project_engineering_plan import (
    EngineeringPlanCriterion,
    EngineeringPlanDecision,
    EngineeringPlanReceipt,
    EngineeringPlanRevision,
    EngineeringPolicyHead,
    EngineeringPolicyRevision,
    ProjectEngineeringPlan,
)
from app.models.system_user import SystemUser
from app.schemas.infrastructure import InfrastructureReference, InfrastructureType
from app.services import infrastructure_catalogue
from app.services.action_readiness import (
    ActionableBlocker,
    ActionReadiness,
    BlockerEvidence,
    ReadinessState,
)
from app.services.audit_adapter import AuditActor, AuditRecord, audit_adapter
from app.services.domain_errors import DomainError
from app.services.events import EventType, emit_event
from app.services.owner_commands import (
    OwnerCommandDefinition,
    execute_owner_command,
    owner_command_active,
)
from app.services.project_engineering_plan_contracts import (
    APPROVE,
    CONCERN,
    OWNER,
    POLICY,
    READ,
    REVOKE,
    SCOPE,
    WRITE,
    AcceptedProjectCompletionEngineeringPreviewBatchQuery,
    AcceptedProjectCompletionPurpose,
    CompletionEngineeringPlanQuery,
    CurrentEngineeringPlanApproval,
    CurrentEngineeringPlanQuery,
    DecideEngineeringPlan,
    DraftEngineeringPlan,
    EngineeringCriterion,
    EngineeringDecisionView,
    EngineeringPlanAction,
    EngineeringPlanContent,
    EngineeringPlanOutcome,
    EngineeringPolicyView,
    EngineeringReadPurpose,
    EngineeringRevisionView,
    EngineeringWorkspace,
    EngineeringWorkspaceQuery,
    IssuanceEngineeringPlanQuery,
    PlanCommand,
    PlanDecisionKind,
    PopSitePlanLocation,
    ProjectDispatchPreview,
    ProjectDispatchPreviewQuery,
    PublishApprovalPolicy,
    SupportCompletionEngineeringBatchQuery,
    SupportCompletionEngineeringPreviewBatchQuery,
    SupportCompletionEngineeringQuery,
)
from app.services.projects import (
    ProjectEngineeringOrigin,
    engineering_plan_origin,
    engineering_plan_revocation_scope,
)
from app.services.staff_permission_admission import (
    AuthorizedStaffPerson,
    revalidate_staff_admission,
)

ERRORS = (
    "denied",
    "not_found",
    "invalid_request",
    "stale_state",
    "idempotency_conflict",
    "unapproved",
)


class EngineeringPlanError(DomainError):
    """Stable transport-neutral engineering refusal."""


def _error(code: str) -> EngineeringPlanError:
    return EngineeringPlanError(code=f"{OWNER}.{code}", message=code.replace("_", " "))


def _utc(value: datetime | None) -> datetime | None:
    return (
        value.replace(tzinfo=UTC)
        if value is not None and value.tzinfo is None
        else value
    )


def _hash(value: object) -> str:
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, separators=(",", ":"), default=str).encode()
    ).hexdigest()


def _binding(
    origin: ProjectEngineeringOrigin, pon_parent_olt_id: UUID | None = None
) -> dict[str, str | int | None]:
    return {
        "project_id": str(origin.project_id),
        "task_id": str(origin.task_id) if origin.task_id else None,
        "project_template_id": str(origin.project_template_id)
        if origin.project_template_id
        else None,
        "applied_template_revision": origin.applied_template_revision,
        "task_template_id": str(origin.task_template_id)
        if origin.task_template_id
        else None,
        "task_template_revision": origin.task_template_revision,
        "infrastructure_type": origin.infrastructure.type.value,
        "infrastructure_id": str(origin.infrastructure.id),
        "pon_parent_olt_id": str(pon_parent_olt_id) if pon_parent_olt_id else None,
    }


def _normalize(content: EngineeringPlanContent) -> EngineeringPlanContent:
    try:
        content = EngineeringPlanContent.model_validate(content.model_dump())
    except ValueError as exc:
        raise _error("invalid_request") from exc
    criteria = tuple(sorted(content.criteria, key=lambda item: item.order))
    if len({item.id for item in criteria}) != len(criteria) or len(
        {item.order for item in criteria}
    ) != len(criteria):
        raise _error("invalid_request")
    if not content.scope_statement.strip() or any(
        not item.statement.strip() or not item.verification_method.strip()
        for item in criteria
    ):
        raise _error("invalid_request")
    if (
        hasattr(content.location, "description")
        and not content.location.description.strip()
    ):
        raise _error("invalid_request")
    return content.model_copy(update={"criteria": criteria})


def _content(db: Session, revision: EngineeringPlanRevision) -> EngineeringPlanContent:
    rows = db.scalars(
        select(EngineeringPlanCriterion)
        .where(EngineeringPlanCriterion.revision_id == revision.id)
        .order_by(EngineeringPlanCriterion.order)
    ).all()
    return EngineeringPlanContent.model_validate(
        {
            **revision.content,
            "criteria": [
                EngineeringCriterion(
                    id=r.id,
                    order=r.order,
                    statement=r.statement,
                    verification_method=r.verification_method,
                )
                for r in rows
            ],
        }
    )


def _digest(
    content: EngineeringPlanContent,
    origin: ProjectEngineeringOrigin,
    pon_parent_olt_id: UUID | None = None,
) -> str:
    return _hash(
        {
            "content": content.model_dump(mode="json"),
            "origin": _binding(origin, pon_parent_olt_id),
        }
    )


def _authorize(
    db: Session, command: PlanCommand, permission: str
) -> AuthorizedStaffPerson:
    context = command.context
    if (
        context.scope != SCOPE
        or context.actor != str(command.access.principal_id)
        or not context.reason.strip()
        or not context.idempotency_key
        or len(context.idempotency_key) > 120
    ):
        raise _error("invalid_request")
    return revalidate_staff_admission(
        db, admission=command.access, permission=permission, write=True
    )


def _authorize_after_waits(
    db: Session, command: PlanCommand, permission: str, actor: AuthorizedStaffPerson
) -> AuthorizedStaffPerson:
    current = _authorize(db, command, permission)
    if current != actor:
        raise _error("denied")
    return current


def _fingerprint(command: PlanCommand) -> str:
    # Correlation/command IDs may change on retry. Actor, reason and all intent
    # fields are decision-bearing and must retain their normalized identity.
    data = asdict(command)
    data.pop("access")
    data["context"] = {
        "actor": command.context.actor,
        "scope": command.context.scope,
        "reason": command.context.reason,
    }
    if isinstance(command, DraftEngineeringPlan):
        data["content"] = _normalize(command.content).model_dump(mode="json")
    return _hash(data)


def _receipt(
    db: Session, command: PlanCommand, fingerprint: str
) -> EngineeringPlanOutcome | None:
    row = db.scalar(
        select(EngineeringPlanReceipt).where(
            EngineeringPlanReceipt.idempotency_key == command.context.idempotency_key
        )
    )
    if row is None:
        return None
    if row.fingerprint != fingerprint:
        raise _error("idempotency_conflict")
    return EngineeringPlanOutcome(
        plan_id=row.plan_id,
        revision_id=row.revision_id,
        decision_id=row.decision_id,
        policy_revision_id=row.policy_revision_id,
        generation=row.generation,
        action=EngineeringPlanAction(row.action),
        digest=row.digest,
        replayed=True,
    )


def _finish(
    db: Session,
    command: PlanCommand,
    fingerprint: str,
    outcome: EngineeringPlanOutcome,
    actor: AuthorizedStaffPerson,
) -> EngineeringPlanOutcome:
    db.add(
        EngineeringPlanReceipt(
            idempotency_key=command.context.idempotency_key,
            fingerprint=fingerprint,
            plan_id=outcome.plan_id,
            revision_id=outcome.revision_id,
            decision_id=outcome.decision_id,
            policy_revision_id=outcome.policy_revision_id,
            generation=outcome.generation,
            action=outcome.action.value,
            digest=outcome.digest,
        )
    )
    details: dict[str, object] = {
        "schema_version": 1,
        "action": outcome.action.value,
        "plan_id": str(outcome.plan_id) if outcome.plan_id else None,
        "revision_id": str(outcome.revision_id) if outcome.revision_id else None,
        "decision_id": str(outcome.decision_id) if outcome.decision_id else None,
        "policy_revision_id": str(outcome.policy_revision_id)
        if outcome.policy_revision_id
        else None,
        "generation": outcome.generation,
        "digest": outcome.digest,
        "command_id": str(command.context.command_id),
        "correlation_id": str(command.context.correlation_id),
        "reason": command.context.reason,
    }
    audit_adapter.stage(
        db,
        AuditRecord(
            action="project_engineering_plan.changed",
            entity_type="project_engineering_plan",
            entity_id=str(outcome.plan_id) if outcome.plan_id else "policy",
            actor=AuditActor.user(str(actor.system_user_id), party_id=actor.person_id),
            details=details,
            metadata=details,
        ),
    )
    emit_event(
        db,
        EventType.project_engineering_plan_changed,
        details,
        actor=command.context.actor,
    )
    db.flush()
    return outcome


def _origin(
    db: Session, command: DraftEngineeringPlan | DecideEngineeringPlan
) -> ProjectEngineeringOrigin:
    origin = engineering_plan_origin(
        db, project_id=command.project_id, task_id=command.task_id, lock=True
    )
    if _utc(origin.project_updated_at) != _utc(
        command.expected_project_updated_at
    ) or _utc(origin.task_updated_at) != _utc(command.expected_task_updated_at):
        raise _error("stale_state")
    return origin


def _head(
    db: Session, command: DraftEngineeringPlan | DecideEngineeringPlan
) -> ProjectEngineeringPlan | None:
    head = db.scalar(
        select(ProjectEngineeringPlan)
        .where(ProjectEngineeringPlan.id == command.plan_id)
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    if head is not None and (
        head.project_id != command.project_id or head.task_id != command.task_id
    ):
        raise _error("invalid_request")
    if (head.generation if head else 0) != command.expected_generation:
        raise _error("stale_state")
    return head


def _scope_locks(db: Session, content: EngineeringPlanContent) -> UUID | None:
    related = (
        (
            InfrastructureReference(
                type=InfrastructureType.location, id=content.location.pop_site_id
            ),
        )
        if isinstance(content.location, PopSitePlanLocation)
        else ()
    )
    scope = infrastructure_catalogue.lock_reference(
        db, reference=content.infrastructure, related=related
    )
    return scope.pon_parent_olt_id


def _location(db: Session, content: EngineeringPlanContent, *, lock: bool) -> None:
    if isinstance(content.location, PopSitePlanLocation):
        query = select(PopSite.id).where(PopSite.id == content.location.pop_site_id)
        if lock:
            query = query.with_for_update()
        if db.scalar(query) is None:
            raise _error("invalid_request")


def _receipt_conflict(exc: IntegrityError) -> None:
    constraint = getattr(getattr(exc.orig, "diag", None), "constraint_name", None)
    if constraint == "engineering_plan_receipts_idempotency_key_key" or (
        constraint is None
        and "engineering_plan_receipts.idempotency_key" in str(exc.orig)
    ):
        raise _error("idempotency_conflict") from exc
    raise exc


def publish_policy(
    db: Session, *, command: PublishApprovalPolicy
) -> EngineeringPlanOutcome:
    try:
        return execute_owner_command(
            db,
            definition=OwnerCommandDefinition(
                owner=OWNER, concern=CONCERN, name="publish_policy"
            ),
            context=command.context,
            operation=lambda: _publish_policy(db, command),
        )
    except IntegrityError as exc:
        _receipt_conflict(exc)
        raise AssertionError("unreachable")


def _publish_policy(
    db: Session, command: PublishApprovalPolicy
) -> EngineeringPlanOutcome:
    actor = _authorize(db, command, POLICY)
    fingerprint = _fingerprint(command)
    replay = _receipt(db, command, fingerprint)
    if replay:
        return replay
    origin = engineering_plan_revocation_scope(
        db, project_id=command.project_id, task_id=None
    )
    head = db.scalar(
        select(EngineeringPolicyHead)
        .where(EngineeringPolicyHead.project_id == command.project_id)
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    actor = _authorize_after_waits(db, command, POLICY, actor)
    fingerprint = _fingerprint(command)
    replay = _receipt(db, command, fingerprint)
    if replay:
        return replay
    if (
        type(command.independent_reviewer_required) is not bool
        or (head.generation if head else 0) != command.expected_generation
        or _utc(origin.project_updated_at) != _utc(command.expected_project_updated_at)
    ):
        raise _error("stale_state")
    if head is None:
        head = EngineeringPolicyHead(project_id=command.project_id, generation=0)
        db.add(head)
        db.flush()
    row = EngineeringPolicyRevision(
        id=uuid4(),
        project_id=command.project_id,
        revision=head.generation + 1,
        independent_reviewer_required=command.independent_reviewer_required,
        actor_user_id=actor.system_user_id,
        actor_person_id=actor.person_id,
        reason=command.context.reason,
        command_id=command.context.command_id,
    )
    db.add(row)
    db.flush()
    head.generation += 1
    head.current_revision_id = row.id
    return _finish(
        db,
        command,
        fingerprint,
        EngineeringPlanOutcome(
            plan_id=None,
            revision_id=None,
            decision_id=None,
            policy_revision_id=row.id,
            generation=head.generation,
            action=EngineeringPlanAction.policy_published,
            digest=None,
        ),
        actor,
    )


def draft_revision(
    db: Session, *, command: DraftEngineeringPlan
) -> EngineeringPlanOutcome:
    try:
        return execute_owner_command(
            db,
            definition=OwnerCommandDefinition(
                owner=OWNER, concern=CONCERN, name="draft_revision"
            ),
            context=command.context,
            operation=lambda: _draft_revision(db, command),
        )
    except IntegrityError as exc:
        _receipt_conflict(exc)
        raise AssertionError("unreachable")


def _draft_revision(
    db: Session, command: DraftEngineeringPlan
) -> EngineeringPlanOutcome:
    actor = _authorize(db, command, WRITE)
    fingerprint = _fingerprint(command)
    replay = _receipt(db, command, fingerprint)
    if replay:
        return replay
    engineering_plan_revocation_scope(
        db, project_id=command.project_id, task_id=command.task_id
    )
    actor = _authorize_after_waits(db, command, WRITE, actor)
    replay = _receipt(db, command, fingerprint)
    if replay:
        return replay
    origin = _origin(db, command)
    head = _head(db, command)
    content = _normalize(command.content)
    if content.infrastructure != origin.infrastructure or (
        head and head.technology != content.technology.value
    ):
        raise _error("invalid_request")
    pon_parent_olt_id = _scope_locks(db, content)
    _location(db, content, lock=False)
    actor = _authorize_after_waits(db, command, WRITE, actor)
    if head is None:
        duplicate = db.scalar(
            select(ProjectEngineeringPlan.id).where(
                ProjectEngineeringPlan.project_id == command.project_id,
                ProjectEngineeringPlan.task_id == command.task_id,
                ProjectEngineeringPlan.technology == content.technology.value,
            )
        )
        if duplicate is not None:
            raise _error("stale_state")
        head = ProjectEngineeringPlan(
            id=command.plan_id,
            project_id=command.project_id,
            task_id=command.task_id,
            technology=content.technology.value,
            generation=0,
        )
        db.add(head)
        db.flush()
    last = (
        db.get(EngineeringPlanRevision, head.latest_revision_id)
        if head.latest_revision_id
        else None
    )
    row = EngineeringPlanRevision(
        id=uuid4(),
        plan_id=head.id,
        revision=(last.revision + 1 if last else 1),
        content=content.model_dump(mode="json", exclude={"criteria"}),
        origin_binding=_binding(origin, pon_parent_olt_id),
        digest=_digest(content, origin, pon_parent_olt_id),
        author_user_id=actor.system_user_id,
        author_person_id=actor.person_id,
        command_id=command.context.command_id,
    )
    db.add(row)
    db.flush()
    for item in content.criteria:
        db.add(EngineeringPlanCriterion(revision_id=row.id, **item.model_dump()))
    head.generation += 1
    head.latest_revision_id = row.id
    return _finish(
        db,
        command,
        fingerprint,
        EngineeringPlanOutcome(
            plan_id=head.id,
            revision_id=row.id,
            decision_id=None,
            policy_revision_id=None,
            generation=head.generation,
            action=EngineeringPlanAction.drafted,
            digest=row.digest,
        ),
        actor,
    )


def decide(db: Session, *, command: DecideEngineeringPlan) -> EngineeringPlanOutcome:
    try:
        return execute_owner_command(
            db,
            definition=OwnerCommandDefinition(
                owner=OWNER, concern=CONCERN, name="decide"
            ),
            context=command.context,
            operation=lambda: _decide(db, command),
        )
    except IntegrityError as exc:
        _receipt_conflict(exc)
        raise AssertionError("unreachable")


def _decide(db: Session, command: DecideEngineeringPlan) -> EngineeringPlanOutcome:
    if command.decision not in {
        PlanDecisionKind.approve,
        PlanDecisionKind.reject,
        PlanDecisionKind.revoke,
    }:
        raise _error("invalid_request")
    actor = _authorize(
        db, command, REVOKE if command.decision == PlanDecisionKind.revoke else APPROVE
    )
    fingerprint = _fingerprint(command)
    replay = _receipt(db, command, fingerprint)
    if replay:
        return replay
    historical = engineering_plan_revocation_scope(
        db, project_id=command.project_id, task_id=command.task_id
    )
    actor = _authorize_after_waits(
        db,
        command,
        REVOKE if command.decision == PlanDecisionKind.revoke else APPROVE,
        actor,
    )
    replay = _receipt(db, command, fingerprint)
    if replay:
        return replay
    origin: ProjectEngineeringOrigin | None = None
    if command.decision == PlanDecisionKind.revoke:
        if _utc(historical.project_updated_at) != _utc(
            command.expected_project_updated_at
        ) or _utc(historical.task_updated_at) != _utc(command.expected_task_updated_at):
            raise _error("stale_state")
    else:
        origin = _origin(db, command)
    head = _head(db, command)
    if head is None:
        raise _error("not_found")
    policy_head = db.scalar(
        select(EngineeringPolicyHead)
        .where(EngineeringPolicyHead.project_id == command.project_id)
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    if (
        policy_head is None
        or policy_head.current_revision_id != command.expected_policy_revision_id
    ):
        raise _error("stale_state")
    policy = db.get(EngineeringPolicyRevision, command.expected_policy_revision_id)
    row = db.get(EngineeringPlanRevision, command.revision_id)
    if (
        policy is None
        or row is None
        or row.plan_id != head.id
        or row.digest != command.expected_digest
    ):
        raise _error("stale_state")
    previous = (
        db.get(EngineeringPlanDecision, head.current_approval_id)
        if head.current_approval_id
        else None
    )
    if command.decision == PlanDecisionKind.revoke:
        if previous is None or previous.revision_id != row.id:
            raise _error("unapproved")
    else:
        if row.id != head.latest_revision_id or (
            previous and previous.revision_id == row.id
        ):
            raise _error("stale_state")
        # One review of a revision; revisions after rejection require new content.
        if (
            db.scalar(
                select(EngineeringPlanDecision.id)
                .where(EngineeringPlanDecision.revision_id == row.id)
                .limit(1)
            )
            is not None
        ):
            raise _error("stale_state")
        assert origin is not None
        content = _content(db, row)
        pon_parent_olt_id = _scope_locks(db, content)
        _location(db, content, lock=False)
        if row.origin_binding != _binding(
            origin, pon_parent_olt_id
        ) or row.digest != _digest(content, origin, pon_parent_olt_id):
            raise _error("stale_state")
        author = db.get(SystemUser, row.author_user_id, populate_existing=True)
        person = db.get(Party, row.author_person_id, populate_existing=True)
        if (
            author is None
            or author.person_party_id != row.author_person_id
            or person is None
            or person.party_type != "person"
            or person.status != "active"
        ):
            raise _error("denied")
        if (
            policy.independent_reviewer_required
            and actor.person_id == row.author_person_id
        ):
            raise _error("denied")

    actor = _authorize_after_waits(
        db,
        command,
        REVOKE if command.decision == PlanDecisionKind.revoke else APPROVE,
        actor,
    )

    def decision(
        kind: PlanDecisionKind, revision: EngineeringPlanRevision, prior_id: UUID | None
    ) -> EngineeringPlanDecision:
        item = EngineeringPlanDecision(
            id=uuid4(),
            plan_id=head.id,
            project_id=head.project_id,
            revision_id=revision.id,
            policy_revision_id=policy.id,
            kind=kind.value,
            digest=revision.digest,
            prior_approval_id=prior_id,
            actor_user_id=actor.system_user_id,
            actor_person_id=actor.person_id,
            author_person_id=revision.author_person_id,
            reason=command.context.reason,
            command_id=command.context.command_id,
        )
        db.add(item)
        db.flush()
        return item

    if command.decision == PlanDecisionKind.approve and previous is not None:
        old = db.get(EngineeringPlanRevision, previous.revision_id)
        assert old is not None
        decision(PlanDecisionKind.supersede, old, previous.id)
    item = decision(command.decision, row, previous.id if previous else None)
    if command.decision == PlanDecisionKind.approve:
        head.current_approval_id = item.id
    elif command.decision == PlanDecisionKind.revoke:
        head.current_approval_id = None
    head.generation += 1
    return _finish(
        db,
        command,
        fingerprint,
        EngineeringPlanOutcome(
            plan_id=head.id,
            revision_id=row.id,
            decision_id=item.id,
            policy_revision_id=policy.id,
            generation=head.generation,
            action=EngineeringPlanAction(command.decision.value),
            digest=row.digest,
        ),
        actor,
    )


def resolve_current(
    db: Session, *, query: CurrentEngineeringPlanQuery
) -> CurrentEngineeringPlanApproval:
    return _resolve_current_locked(db, query=query, permission=READ, write=False)


def stage_current_for_issuance(
    db: Session, *, query: IssuanceEngineeringPlanQuery
) -> CurrentEngineeringPlanApproval:
    """Flush-free participant: dispatch authority is distinct from plan review."""
    if not owner_command_active(db, owner="operations.project_work_order_handoff"):
        raise _error("invalid_request")
    return _resolve_current_locked(
        db, query=query, permission="operations:dispatch:write", write=True
    )


def stage_current_for_completion(
    db: Session, *, query: CompletionEngineeringPlanQuery
) -> CurrentEngineeringPlanApproval:
    """Fresh staff review participant; completion is distinct from new issuance."""
    if not owner_command_active(db, owner="operations.field_completion"):
        raise _error("invalid_request")
    return _resolve_current_locked(
        db,
        query=query,
        permission="operations:completion-review:write",
        write=True,
        require_issuance_eligibility=False,
    )


def resolve_current_for_completion(
    db: Session, *, query: CompletionEngineeringPlanQuery
) -> CurrentEngineeringPlanApproval:
    """Narrow current-evidence read; immutable history has no freshness gate."""
    return _resolve_current_locked(
        db,
        query=query,
        permission="operations:completion-review:read",
        write=False,
        require_issuance_eligibility=False,
    )


def resolve_current_for_completion_review(
    db: Session, *, query: CompletionEngineeringPlanQuery
) -> CurrentEngineeringPlanApproval:
    """Narrow staff decision preview under completion WRITE, without READ floor."""
    return _resolve_current_locked(
        db,
        query=query,
        permission="operations:completion-review:write",
        write=True,
        require_issuance_eligibility=False,
    )


def _resolve_current_locked(
    db: Session,
    *,
    query: CurrentEngineeringPlanQuery,
    permission: str,
    write: bool,
    require_issuance_eligibility: bool = True,
) -> CurrentEngineeringPlanApproval:
    reader = revalidate_staff_admission(
        db, admission=query.access, permission=permission, write=write
    )
    result = _resolve_exact_current(
        db, query=query, require_issuance_eligibility=require_issuance_eligibility
    )
    if (
        revalidate_staff_admission(
            db, admission=query.access, permission=permission, write=write
        )
        != reader
    ):
        raise _error("denied")
    return result


def stage_current_for_support_completion(
    db: Session, *, query: SupportCompletionEngineeringQuery
) -> CurrentEngineeringPlanApproval:
    """Only the named support lifecycle owner may consume exact completion freshness."""
    if not owner_command_active(db, owner="support.ticket_lifecycle"):
        raise _error("participant_required")
    return _resolve_exact_current(db, query=query, require_issuance_eligibility=False)


def stage_current_batch_for_support_completion(
    db: Session, *, query: SupportCompletionEngineeringBatchQuery
) -> tuple[CurrentEngineeringPlanApproval, ...]:
    """Lock all roots, plans and native targets before validating exact approvals."""
    if not owner_command_active(db, owner="support.ticket_lifecycle"):
        raise _error("participant_required")
    return _support_completion_batch_locked(db, query=query)


def preview_current_batch_for_support_completion(
    db: Session, *, query: SupportCompletionEngineeringPreviewBatchQuery
) -> tuple[CurrentEngineeringPlanApproval, ...]:
    actor = revalidate_staff_admission(
        db, admission=query.access, permission="support:ticket:update", write=True
    )
    result = _support_completion_batch_locked(db, query=query)
    if (
        revalidate_staff_admission(
            db, admission=query.access, permission="support:ticket:update", write=True
        )
        != actor
    ):
        raise _error("denied")
    return result


def _support_completion_batch_locked(
    db: Session, *, query: SupportCompletionEngineeringBatchQuery
) -> tuple[CurrentEngineeringPlanApproval, ...]:
    if not query.plans:
        return ()
    for project_id in sorted({item.project_id for item in query.plans}):
        engineering_plan_revocation_scope(db, project_id=project_id, task_id=None)
    for item in sorted(
        query.plans, key=lambda item: (str(item.project_id), str(item.task_id))
    ):
        if item.task_id is not None:
            engineering_plan_revocation_scope(
                db, project_id=item.project_id, task_id=item.task_id
            )
    db.scalars(
        select(ProjectEngineeringPlan)
        .where(ProjectEngineeringPlan.id.in_({item.plan_id for item in query.plans}))
        .order_by(ProjectEngineeringPlan.id)
        .with_for_update()
        .execution_options(populate_existing=True)
    ).all()
    references: dict[tuple[str, str], InfrastructureReference] = {}
    for item in query.plans:
        revision = db.get(EngineeringPlanRevision, item.expected_revision_id)
        if revision is None:
            raise _error("unapproved")
        content = _content(db, revision)
        reference = content.infrastructure
        references[(reference.type.value, str(reference.id))] = reference
        if isinstance(content.location, PopSitePlanLocation):
            reference = InfrastructureReference(
                type=InfrastructureType.location, id=content.location.pop_site_id
            )
            references[(reference.type.value, str(reference.id))] = reference
    ordered = tuple(references[key] for key in sorted(references))
    if ordered:
        # The canonical catalogue locks ALL supplied targets plus native PON
        # parents globally by table/UUID and rechecks parent bindings after waits.
        infrastructure_catalogue.lock_reference(
            db, reference=ordered[0], related=ordered[1:]
        )
    return tuple(
        _resolve_exact_current(db, query=item, require_issuance_eligibility=False)
        for item in query.plans
    )


def _resolve_exact_current(
    db: Session,
    *,
    query: CurrentEngineeringPlanQuery | SupportCompletionEngineeringQuery,
    require_issuance_eligibility: bool,
) -> CurrentEngineeringPlanApproval:
    origin = engineering_plan_origin(
        db, project_id=query.project_id, task_id=query.task_id, lock=True
    )
    if require_issuance_eligibility and not origin.issuance_eligible:
        raise _error("unapproved")
    head = db.scalar(
        select(ProjectEngineeringPlan)
        .where(ProjectEngineeringPlan.id == query.plan_id)
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    if (
        head is None
        or head.project_id != query.project_id
        or head.task_id != query.task_id
        or head.current_approval_id != query.expected_approval_id
    ):
        raise _error("unapproved")
    approval = db.get(EngineeringPlanDecision, head.current_approval_id)
    row = db.get(EngineeringPlanRevision, query.expected_revision_id)
    if (
        approval is None
        or approval.kind != "approve"
        or approval.revision_id != query.expected_revision_id
        or row is None
        or row.plan_id != head.id
        or row.digest != query.expected_digest
        or approval.digest != row.digest
    ):
        raise _error("unapproved")
    policy_head = db.get(
        EngineeringPolicyHead, query.project_id, populate_existing=True
    )
    policy = db.get(EngineeringPolicyRevision, approval.policy_revision_id)
    if (
        policy_head is None
        or policy_head.current_revision_id is None
        or policy is None
        or policy.project_id != query.project_id
        or (
            policy.independent_reviewer_required
            and approval.actor_person_id == row.author_person_id
        )
    ):
        raise _error("unapproved")
    content = _content(db, row)
    pon_parent_olt_id = _scope_locks(db, content)
    _location(db, content, lock=False)
    if (
        row.origin_binding != _binding(origin, pon_parent_olt_id)
        or row.digest != _digest(content, origin, pon_parent_olt_id)
        or content.infrastructure != origin.infrastructure
    ):
        raise _error("stale_state")
    return CurrentEngineeringPlanApproval(
        plan_id=head.id,
        project_id=head.project_id,
        task_id=head.task_id,
        revision_id=row.id,
        approval_id=approval.id,
        policy_revision_id=approval.policy_revision_id,
        digest=row.digest,
        content=content,
        author_person_id=row.author_person_id,
        reviewer_person_id=approval.actor_person_id,
        generation=head.generation,
    )


# Protected read capabilities expose only the evidence needed by their action.
# No command executor, receipt, mutation or broadened dispatch history access.
def _readiness(action: str, project_id: UUID, reason: str | None) -> ActionReadiness:
    blockers = (
        ()
        if reason is None
        else (
            ActionableBlocker(
                code=f"{OWNER}.{action}.blocked",
                owner=OWNER,
                customer_message="Engineering review required",
                staff_detail=reason,
                evidence=BlockerEvidence(summary=reason),
            ),
        )
    )
    return ActionReadiness(
        action_key=f"project_engineering.{action}",
        subject_type="project",
        subject_id=str(project_id),
        owner=OWNER,
        state=ReadinessState.ready if not blockers else ReadinessState.blocked,
        evaluated_at=datetime.now(UTC),
        blockers=blockers,
    )


def _revision_view(
    db: Session, row: EngineeringPlanRevision
) -> EngineeringRevisionView:
    return EngineeringRevisionView(
        id=row.id,
        revision=row.revision,
        digest=row.digest,
        author_person_id=row.author_person_id,
        created_at=_utc(row.created_at) or datetime.now(UTC),
        content=_content(db, row),
    )


def read_workspace(
    db: Session, *, query: EngineeringWorkspaceQuery
) -> EngineeringWorkspace:
    permission = {
        EngineeringReadPurpose.overview: READ,
        EngineeringReadPurpose.policy: POLICY,
        EngineeringReadPurpose.draft: WRITE,
        EngineeringReadPurpose.review: APPROVE,
        EngineeringReadPurpose.revoke: REVOKE,
    }[query.purpose]
    actor = revalidate_staff_admission(
        db,
        admission=query.access,
        permission=permission,
        write=query.purpose != EngineeringReadPurpose.overview,
    )
    if query.history_offset < 0 or query.history_offset > 100000:
        raise _error("invalid_request")
    historical = engineering_plan_revocation_scope(
        db, project_id=query.project_id, task_id=query.task_id
    )
    origin: ProjectEngineeringOrigin | None = None
    origin_error: str | None = None
    if query.purpose in {EngineeringReadPurpose.draft, EngineeringReadPurpose.review}:
        try:
            origin = engineering_plan_origin(
                db, project_id=query.project_id, task_id=query.task_id, lock=True
            )
        except DomainError as exc:
            origin_error = exc.message
    head = db.scalar(
        select(ProjectEngineeringPlan)
        .where(
            ProjectEngineeringPlan.project_id == query.project_id,
            ProjectEngineeringPlan.task_id == query.task_id,
            ProjectEngineeringPlan.technology == query.technology.value,
        )
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    policy_head = db.scalar(
        select(EngineeringPolicyHead)
        .where(EngineeringPolicyHead.project_id == query.project_id)
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    policy = (
        db.get(EngineeringPolicyRevision, policy_head.current_revision_id)
        if policy_head and policy_head.current_revision_id
        else None
    )
    policy_view = (
        EngineeringPolicyView(
            id=policy.id,
            revision=policy.revision,
            independent_reviewer_required=policy.independent_reviewer_required,
            reason=policy.reason,
            created_at=_utc(policy.created_at) or datetime.now(UTC),
        )
        if policy
        else None
    )
    approval = (
        db.get(EngineeringPlanDecision, head.current_approval_id)
        if head and head.current_approval_id
        else None
    )
    revision_id = (
        approval.revision_id
        if query.purpose == EngineeringReadPurpose.revoke and approval
        else head.latest_revision_id
        if head
        else None
    )
    if query.selected_revision_id is not None:
        if query.purpose != EngineeringReadPurpose.overview:
            raise _error("invalid_request")
        revision_id = query.selected_revision_id
    revision = db.get(EngineeringPlanRevision, revision_id) if revision_id else None
    if query.selected_revision_id is not None and (
        revision is None or head is None or revision.plan_id != head.id
    ):
        raise _error("not_found")
    reason = origin_error
    if query.purpose == EngineeringReadPurpose.review:
        if revision is None or policy is None:
            reason = (
                "A draft revision and explicit project approval policy are required."
            )
        elif (
            db.scalar(
                select(EngineeringPlanDecision.id)
                .where(EngineeringPlanDecision.revision_id == revision.id)
                .limit(1)
            )
            is not None
        ):
            reason = "This revision was already reviewed. Draft a new revision before review."
        elif reason is None and origin is not None:
            content = _content(db, revision)
            try:
                parent_id = _scope_locks(db, content)
                _location(db, content, lock=False)
                if revision.origin_binding != _binding(
                    origin, parent_id
                ) or revision.digest != _digest(content, origin, parent_id):
                    reason = (
                        "Engineering scope changed. Draft and review a new revision."
                    )
            except DomainError as exc:
                reason = exc.message
            author = db.get(SystemUser, revision.author_user_id, populate_existing=True)
            person = db.get(Party, revision.author_person_id, populate_existing=True)
            if (
                author is None
                or author.person_party_id != revision.author_person_id
                or person is None
                or person.party_type != "person"
                or person.status != "active"
            ):
                reason = (
                    "The draft author must retain the canonical active Person binding."
                )
            elif (
                policy.independent_reviewer_required
                and actor.person_id == revision.author_person_id
            ):
                reason = "This project's selected policy requires a different reviewing Person."
    if query.purpose == EngineeringReadPurpose.revoke:
        if approval is None:
            reason = "There is no current approval to revoke."
        elif policy is None:
            reason = (
                "The explicit current project policy is required for this decision."
            )
    if query.purpose == EngineeringReadPurpose.policy and query.task_id is not None:
        raise _error("invalid_request")
    history: tuple[EngineeringRevisionView, ...] = ()
    decisions: tuple[EngineeringDecisionView, ...] = ()
    has_more = False
    if query.purpose == EngineeringReadPurpose.overview and head:
        rows = db.scalars(
            select(EngineeringPlanRevision)
            .where(EngineeringPlanRevision.plan_id == head.id)
            .order_by(EngineeringPlanRevision.revision.desc())
            .offset(query.history_offset)
            .limit(21)
        ).all()
        has_more = len(rows) > 20
        history = tuple(_revision_view(db, row) for row in rows[:20])
        revision_ids = tuple(item.id for item in history)
        decisions = tuple(
            EngineeringDecisionView(
                id=item.id,
                revision_id=item.revision_id,
                kind=PlanDecisionKind(item.kind),
                actor_person_id=item.actor_person_id,
                reason=item.reason,
                created_at=_utc(item.created_at) or datetime.now(UTC),
            )
            for item in db.scalars(
                select(EngineeringPlanDecision)
                .where(
                    EngineeringPlanDecision.plan_id == head.id,
                    EngineeringPlanDecision.revision_id.in_(revision_ids),
                )
                .order_by(
                    EngineeringPlanDecision.created_at, EngineeringPlanDecision.id
                )
            )
        )
    if (
        revalidate_staff_admission(
            db,
            admission=query.access,
            permission=permission,
            write=query.purpose != EngineeringReadPurpose.overview,
        )
        != actor
    ):
        raise _error("denied")
    return EngineeringWorkspace(
        project_id=query.project_id,
        task_id=query.task_id,
        project_label=historical.project_label,
        task_label=historical.task_label,
        technology=query.technology,
        purpose=query.purpose,
        project_updated_at=_utc(historical.project_updated_at)
        or historical.project_updated_at,
        task_updated_at=_utc(historical.task_updated_at),
        infrastructure=None
        if query.purpose == EngineeringReadPurpose.policy
        else origin.infrastructure
        if origin
        else _content(db, revision).infrastructure
        if revision
        else None,
        plan_id=head.id
        if head and query.purpose != EngineeringReadPurpose.policy
        else None,
        generation=head.generation if head else 0,
        policy_generation=policy_head.generation if policy_head else 0,
        policy=policy_view,
        revision=_revision_view(db, revision)
        if revision and query.purpose != EngineeringReadPurpose.policy
        else None,
        approval_id=approval.id
        if approval and query.purpose != EngineeringReadPurpose.policy
        else None,
        history=history,
        decisions=decisions,
        history_offset=query.history_offset,
        history_has_more=has_more,
        readiness=_readiness(query.purpose.value, query.project_id, reason),
        evaluated_at=datetime.now(UTC),
    )


def preview_dispatch(
    db: Session, *, query: ProjectDispatchPreviewQuery
) -> ProjectDispatchPreview:
    """Narrow GET capability: current-approved evidence only, no draft/history read."""
    permission = "operations:dispatch:write"
    actor = revalidate_staff_admission(
        db, admission=query.access, permission=permission, write=True
    )
    historical = engineering_plan_revocation_scope(
        db, project_id=query.project_id, task_id=query.task_id
    )
    approval: CurrentEngineeringPlanApproval | None = None
    reason: str | None = None
    head = db.scalar(
        select(ProjectEngineeringPlan)
        .where(
            ProjectEngineeringPlan.project_id == query.project_id,
            ProjectEngineeringPlan.task_id == query.task_id,
            ProjectEngineeringPlan.technology == query.technology.value,
        )
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    current = (
        db.get(EngineeringPlanDecision, head.current_approval_id)
        if head and head.current_approval_id
        else None
    )
    if head is None or current is None:
        reason = "Approve an engineering revision for this exact Project and task before issuance."
    else:
        try:
            approval = _resolve_current_locked(
                db,
                query=CurrentEngineeringPlanQuery(
                    access=query.access,
                    plan_id=head.id,
                    project_id=query.project_id,
                    task_id=query.task_id,
                    expected_revision_id=current.revision_id,
                    expected_approval_id=current.id,
                    expected_digest=current.digest,
                ),
                permission=permission,
                write=True,
            )
        except EngineeringPlanError as exc:
            reason = exc.message
        except DomainError as exc:
            reason = exc.message
    if (
        revalidate_staff_admission(
            db, admission=query.access, permission=permission, write=True
        )
        != actor
    ):
        raise _error("denied")
    return ProjectDispatchPreview(
        project_id=query.project_id,
        task_id=query.task_id,
        project_label=historical.project_label,
        task_label=historical.task_label,
        technology=query.technology,
        project_updated_at=_utc(historical.project_updated_at)
        or historical.project_updated_at,
        task_updated_at=_utc(historical.task_updated_at),
        approval=approval,
        readiness=_readiness("issue", query.project_id, reason),
        evaluated_at=datetime.now(UTC),
    )


def stage_current_batch_for_accepted_project_completion(
    db: Session, *, query: SupportCompletionEngineeringBatchQuery
) -> tuple[CurrentEngineeringPlanApproval, ...]:
    """Accepted project completion participant; does not grant review or issuance authority."""
    if not owner_command_active(db, owner="operations.field_completion"):
        raise _error("participant_required")
    return _support_completion_batch_locked(db, query=query)


def preview_current_batch_for_accepted_project_completion(
    db: Session, *, query: AcceptedProjectCompletionEngineeringPreviewBatchQuery
) -> tuple[CurrentEngineeringPlanApproval, ...]:
    permission = (
        "project:task:write"
        if query.purpose == AcceptedProjectCompletionPurpose.task
        else "project:update"
    )
    actor = revalidate_staff_admission(
        db, admission=query.access, permission=permission, write=True
    )
    result = _support_completion_batch_locked(db, query=query)
    if (
        revalidate_staff_admission(
            db, admission=query.access, permission=permission, write=True
        )
        != actor
    ):
        raise _error("denied")
    return result
