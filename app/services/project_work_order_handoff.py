"""One atomic issuance coordinator; native owners retain approval and header writes."""

import hashlib
import json
from dataclasses import asdict
from datetime import UTC, datetime
from uuid import NAMESPACE_URL, UUID, uuid5

from sqlalchemy import select, text
from sqlalchemy.orm import Session

from app.models.project_work_order_issuance import ProjectWorkOrderIssuance
from app.schemas.infrastructure import InfrastructureReference, InfrastructureType
from app.services.audit_adapter import AuditActor, AuditRecord, audit_adapter
from app.services.domain_errors import DomainError
from app.services.events import EventType, emit_event
from app.services.owner_commands import OwnerCommandDefinition, execute_owner_command
from app.services.project_engineering_plan_contracts import (
    EngineeringTechnology,
    IssuanceEngineeringPlanQuery,
)
from app.services.project_engineering_plans import stage_current_for_issuance
from app.services.project_work_order_handoff_contracts import (
    CONCERN,
    OWNER,
    PERMISSION,
    CompletionIssuanceEvidence,
    CompletionIssuanceEvidenceQuery,
    CompletionIssuancePurpose,
    IssueProjectWorkOrder,
    ProjectWorkOrderIssued,
    ProjectWorkOrderIssuedEvent,
)
from app.services.projects import engineering_plan_origin
from app.services.staff_permission_admission import revalidate_staff_admission
from app.services.work_order_commands import WorkOrderCommands
from app.services.work_order_creation_contracts import (
    ProjectInfrastructureProvenance,
    ProjectNativeWorkOrderCreateCommand,
)
from app.services.work_order_templates import WorkOrderPurpose


def _error(code: str) -> DomainError:
    return DomainError(code=f"{OWNER}.{code}", message=code.replace("_", " "))


def _utc(value: datetime | None) -> datetime | None:
    return (
        value.replace(tzinfo=UTC)
        if value is not None and value.tzinfo is None
        else value
    )


def _fingerprint(command: IssueProjectWorkOrder) -> str:
    # Context transport IDs may change on retry; actor, scope, reason and all intent fields may not.
    values = asdict(command)
    values.pop("access")
    values["context"] = {
        "actor": command.context.actor,
        "scope": command.context.scope,
        "reason": command.context.reason,
    }
    values["infrastructure"] = command.infrastructure.model_dump(mode="json")
    values["location"] = command.location.model_dump(mode="json")
    return hashlib.sha256(
        json.dumps(values, sort_keys=True, separators=(",", ":"), default=str).encode()
    ).hexdigest()


def _outcome(
    row: ProjectWorkOrderIssuance, *, replayed: bool
) -> ProjectWorkOrderIssued:
    return ProjectWorkOrderIssued(
        issuance_id=row.id,
        work_order_id=row.work_order_id,
        public_id=row.public_id,
        project_id=row.project_id,
        task_id=row.task_id,
        plan_id=row.plan_id,
        revision_id=row.revision_id,
        approval_id=row.approval_id,
        digest=row.digest,
        command_id=row.command_id,
        issued_at=row.issued_at,
        replayed=replayed,
    )


def issue(db: Session, *, command: IssueProjectWorkOrder) -> ProjectWorkOrderIssued:
    return execute_owner_command(
        db,
        definition=OwnerCommandDefinition(owner=OWNER, concern=CONCERN, name="issue"),
        context=command.context,
        operation=lambda: _stage(db, command=command),
    )


def _stage(db: Session, *, command: IssueProjectWorkOrder) -> ProjectWorkOrderIssued:
    actor = revalidate_staff_admission(
        db, admission=command.access, permission=PERMISSION, write=True
    )
    context = command.context
    key = context.idempotency_key
    if (
        context.actor != str(actor.system_user_id)
        or context.scope != PERMISSION
        or not key
        or not key.strip()
        or len(key) > 120
    ):
        raise _error("invalid_request")
    fingerprint = _fingerprint(command)
    # Serialize owner-global receipt keys before origin locks. Hash collisions only serialize unrelated keys.
    if db.get_bind().dialect.name == "postgresql":
        db.execute(
            text("SELECT pg_advisory_xact_lock(:key)"),
            {
                "key": int.from_bytes(
                    hashlib.sha256((OWNER + key).encode()).digest()[:8],
                    "big",
                    signed=True,
                )
            },
        )
    row = db.scalar(
        select(ProjectWorkOrderIssuance).where(
            ProjectWorkOrderIssuance.idempotency_key == key
        )
    )
    if row is not None:
        if (
            revalidate_staff_admission(
                db, admission=command.access, permission=PERMISSION, write=True
            )
            != actor
        ):
            raise _error("denied")
        if row.fingerprint != fingerprint:
            raise _error("idempotency_conflict")
        return _outcome(row, replayed=True)
    origin = engineering_plan_origin(
        db, project_id=command.project_id, task_id=command.task_id, lock=True
    )
    if (
        not origin.issuance_eligible
        or _utc(origin.project_updated_at) != _utc(command.expected_project_updated_at)
        or _utc(origin.task_updated_at) != _utc(command.expected_task_updated_at)
    ):
        raise _error("stale_state")
    approval = stage_current_for_issuance(
        db,
        query=IssuanceEngineeringPlanQuery(
            access=command.access,
            plan_id=command.plan_id,
            project_id=command.project_id,
            task_id=command.task_id,
            expected_revision_id=command.revision_id,
            expected_approval_id=command.approval_id,
            expected_digest=command.digest,
        ),
    )
    if (
        approval.content.infrastructure != command.infrastructure
        or approval.content.location != command.location
        or approval.content.technology.value
        != command.classification.access_technology.value
    ):
        raise _error("scope_mismatch")
    if (
        revalidate_staff_admission(
            db, admission=command.access, permission=PERMISSION, write=True
        )
        != actor
    ):
        raise _error("denied")
    created = WorkOrderCommands.stage_native_create(
        db,
        command=ProjectNativeWorkOrderCreateCommand(
            public_id=f"sub-{uuid5(NAMESPACE_URL, OWNER + ':' + key).hex}",
            title=command.title,
            description=command.description,
            priority=command.priority,
            provenance=ProjectInfrastructureProvenance(
                approval=approval,
                classification=command.classification,
                issuer_person_id=actor.person_id,
            ),
            access=command.access,
        ),
        context=context,
    )
    row = ProjectWorkOrderIssuance(
        idempotency_key=key,
        fingerprint=fingerprint,
        work_order_id=created.id,
        public_id=created.public_id,
        project_id=approval.project_id,
        task_id=approval.task_id,
        plan_id=approval.plan_id,
        revision_id=approval.revision_id,
        approval_id=approval.approval_id,
        digest=approval.digest,
        infrastructure_type=command.infrastructure.type.value,
        infrastructure_id=command.infrastructure.id,
        purpose=command.classification.purpose.value,
        technology=command.classification.access_technology.value,
        actor_user_id=actor.system_user_id,
        actor_person_id=actor.person_id,
        reason=context.reason,
        command_id=context.command_id,
    )
    db.add(row)
    db.flush()
    outcome = _outcome(row, replayed=False)
    event = ProjectWorkOrderIssuedEvent(
        issuance_id=row.id,
        work_order_id=row.work_order_id,
        project_id=row.project_id,
        task_id=row.task_id,
        plan_id=row.plan_id,
        revision_id=row.revision_id,
        approval_id=row.approval_id,
        digest=row.digest,
        command_id=context.command_id,
        correlation_id=context.correlation_id,
        reason=context.reason,
    )
    details: dict[str, object] = {
        name: str(value) if isinstance(value, UUID) else value
        for name, value in asdict(event).items()
    }

    audit_adapter.stage(
        db,
        AuditRecord(
            action="project_work_order.issued",
            entity_type="work_order",
            entity_id=str(row.work_order_id),
            actor=AuditActor.user(str(actor.system_user_id), party_id=actor.person_id),
            details=details,
            metadata=details,
        ),
    )
    emit_event(db, EventType.project_work_order_issued, details, actor=context.actor)
    db.flush()
    return outcome


def completion_issuance_evidence(
    db: Session, *, query: CompletionIssuanceEvidenceQuery
) -> CompletionIssuanceEvidence:
    """Protected immutable receipt read, distinct from permission to issue new work."""
    if query.purpose not in {
        CompletionIssuancePurpose.read,
        CompletionIssuancePurpose.review,
    }:
        raise _error("scope_mismatch")
    permission = (
        "operations:completion-review:read"
        if query.purpose == CompletionIssuancePurpose.read
        else "operations:completion-review:write"
    )
    actor = revalidate_staff_admission(
        db,
        admission=query.access,
        permission=permission,
        write=query.purpose == CompletionIssuancePurpose.review,
    )
    row = db.scalar(
        select(ProjectWorkOrderIssuance)
        .where(ProjectWorkOrderIssuance.id == query.issuance_id)
        .execution_options(populate_existing=True)
    )
    if (
        row is None
        or row.work_order_id != query.expected_work_order_id
        or row.public_id != query.expected_public_id
    ):
        raise _error("scope_mismatch")
    try:
        evidence = CompletionIssuanceEvidence(
            issuance_id=row.id,
            work_order_id=row.work_order_id,
            public_id=row.public_id,
            project_id=row.project_id,
            task_id=row.task_id,
            plan_id=row.plan_id,
            revision_id=row.revision_id,
            approval_id=row.approval_id,
            digest=row.digest,
            infrastructure=InfrastructureReference(
                type=InfrastructureType(row.infrastructure_type),
                id=row.infrastructure_id,
            ),
            purpose=WorkOrderPurpose(row.purpose),
            technology=EngineeringTechnology(row.technology),
        )
    except ValueError as exc:
        raise _error("scope_mismatch") from exc
    if (
        revalidate_staff_admission(
            db,
            admission=query.access,
            permission=permission,
            write=query.purpose == CompletionIssuancePurpose.review,
        )
        != actor
    ):
        raise _error("denied")
    return evidence
