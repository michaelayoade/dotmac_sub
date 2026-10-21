"""Authoritative native work-order and dispatch command owner.

Adapters may authorize and shape requests, but native work-order creation,
header mutation, assignment projection, and assignment-queue transitions are
decided and committed here. CRM mirror ingest remains a separate observation
boundary and field execution status transitions remain owned by
``app.services.field.transitions``.
"""

from __future__ import annotations

import hashlib
import json
import uuid
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from typing import Any, overload

from fastapi import HTTPException
from fastapi.encoders import jsonable_encoder
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session
from sqlalchemy.sql.elements import ColumnElement

from app.models.audit import AuditActorType, AuditEvent
from app.models.dispatch import (
    DispatchQueueStatus,
    DispatchRule,
    TechnicianProfile,
    WorkOrderAssignmentQueue,
    WorkOrderAssignmentReceipt,
)
from app.models.field_vendor import FieldVendor, FieldVendorUser
from app.models.project import Project, ProjectTask
from app.models.subscriber import Subscriber
from app.models.support import Ticket, canonical_ticket_status_value
from app.models.system_user import SystemUser
from app.models.vendor_routes import Vendor
from app.models.work_order import WorkOrder
from app.schemas.dispatch import (
    WorkOrderAssignmentQueueCreate,
    WorkOrderAssignmentQueueRead,
    WorkOrderAssignmentQueueUpdate,
    WorkOrderHeaderCreate,
    WorkOrderHeaderUpdate,
)
from app.schemas.network import InfrastructureWorkOrderHeaderCreate
from app.services.audit_adapter import AuditActor, stage_audit_event
from app.services.common import coerce_uuid
from app.services.events import EventType, emit_event
from app.services.field.source import mark_sub_authoritative
from app.services.field.work_order_kind import WorkOrderKind
from app.services.field.work_order_status import (
    TERMINAL_WORK_ORDER_STATUSES,
    WORK_ORDER_STATUSES,
    WorkOrderStatus,
)
from app.services.operator_tenant import OPERATOR_TENANT_ID
from app.services.owner_commands import (
    CommandContext,
    OwnerCommandDefinition,
    execute_owner_command,
)
from app.services.permission_admission import PermissionAdmission
from app.services.work_order_assignment_contracts import (
    AssignmentOperation,
    AssignmentTarget,
    CompletionEnrollmentLinkage,
    CompletionEnrollmentLinkagePreviewQuery,
    CompletionEnrollmentLinkageQuery,
    CurrentWorkOrderAssignment,
    CurrentWorkOrderAssignmentQuery,
    TechnicianAssignmentTarget,
    VendorAssignmentCandidate,
    VendorAssignmentCandidates,
    VendorAssignmentCandidatesQuery,
    VendorAssignmentTarget,
    WorkOrderAssignmentCommand,
    WorkOrderAssignmentEligibility,
    WorkOrderAssignmentEligibilityQuery,
    WorkOrderAssignmentOutcome,
    WorkOrderAssignmentPreparation,
    WorkOrderAssignmentPreparationQuery,
    WorkOrderAssignmentPreview,
    WorkOrderAssignmentQuery,
    WorkOrderAssignmentReplaySnapshot,
    WorkOrderAssignmentState,
    WorkOrderLinkageFence,
)
from app.services.work_order_classification_contracts import (
    ClassificationEligibility,
    ClassifyWorkOrderCommand,
    WorkOrderClassificationOutcome,
    WorkOrderClassificationQuery,
    WorkOrderClassificationSnapshot,
    WorkOrderClassifiedEventPayload,
)
from app.services.work_order_creation_contracts import (
    NativeWorkOrderCreateCommand,
    NativeWorkOrderCreated,
    NativeWorkOrderSnapshot,
    ProjectNativeWorkOrderCreateCommand,
    ProjectNativeWorkOrderCreated,
)
from app.services.work_order_errors import WorkOrderCommandError
from app.services.work_order_templates import (
    ClassificationSource,
    ExplicitWorkOrderClassification,
    TemplateErrorCode,
    WorkOrderAccessTechnology,
    WorkOrderPurpose,
    WorkOrderTemplateError,
    WorkOrderTemplateQuery,
    recommend_template,
)


def technician_header_assignment(
    profile: TechnicianProfile,
) -> ColumnElement[bool] | None:
    """Adapt the retained staff assignment projection without granting vendor scope."""
    if profile.crm_person_id:
        return WorkOrder.assigned_to_crm_person_id == profile.crm_person_id
    return None


_CREATE_ID_NAMESPACE = uuid.UUID("cbf90ef0-a977-49fb-a2ac-a636eb3b2342")
_QUEUE_STATUSES = frozenset(
    {
        DispatchQueueStatus.queued,
        DispatchQueueStatus.assigned,
        DispatchQueueStatus.skipped,
    }
)
_INITIAL_STATUSES = frozenset(
    {WorkOrderStatus.draft.value, WorkOrderStatus.scheduled.value}
)
_FIELD_EXECUTION_STATUSES = frozenset(
    {
        WorkOrderStatus.in_progress.value,
        WorkOrderStatus.paused.value,
        WorkOrderStatus.completed.value,
    }
)
_ASSIGNMENT_HEADER_FIELDS = frozenset(
    {
        "assigned_to_crm_person_id",
        "assigned_to_name",
        "technician_name",
        "technician_phone",
    }
)


@dataclass(frozen=True)
class AutomatedProjectTaskWorkOrderCommand:
    """Typed participant command for configured ProjectTask field work."""

    project_id: uuid.UUID
    project_task_id: uuid.UUID
    subscriber_id: uuid.UUID
    title: str
    description: str | None
    priority: str | None
    address: str | None
    requires_as_built_evidence: bool
    idempotency_key: str


def _data(payload: Any, *, exclude_unset: bool = False) -> dict[str, Any]:
    if hasattr(payload, "model_dump"):
        return payload.model_dump(exclude_unset=exclude_unset)
    return dict(payload)


def _fingerprint(data: dict[str, Any]) -> str:
    def canonical(value: Any) -> Any:
        if isinstance(value, datetime):
            normalized = (
                value.replace(tzinfo=UTC)
                if value.tzinfo is None
                else value.astimezone(UTC)
            )
            return normalized.isoformat()
        if isinstance(value, dict):
            return {key: canonical(item) for key, item in value.items()}
        if isinstance(value, (list, tuple)):
            return [canonical(item) for item in value]
        return value

    encoded = json.dumps(
        canonical(data),
        default=str,
        separators=(",", ":"),
        sort_keys=True,
    ).encode()
    return hashlib.sha256(encoded).hexdigest()


def _actor(auth: dict[str, Any] | None) -> tuple[AuditActorType, str | None]:
    if not auth:
        return AuditActorType.system, None
    principal_type = str(auth.get("principal_type") or "").strip().lower()
    actor_type = {
        "api_key": AuditActorType.api_key,
        "service": AuditActorType.service,
        "system_user": AuditActorType.user,
        "subscriber": AuditActorType.user,
        "reseller_user": AuditActorType.user,
    }.get(principal_type, AuditActorType.system)
    actor_id = auth.get("principal_id") or auth.get("actor_id")
    return actor_type, str(actor_id) if actor_id else None


def _audit(
    db: Session,
    *,
    action: str,
    work_order: WorkOrder,
    auth: dict[str, Any] | None,
    request_id: str | None,
    metadata: dict[str, object],
) -> None:
    actor_type, actor_id = _actor(auth)
    stage_audit_event(
        db,
        action=action,
        entity_type="work_order",
        entity_id=work_order.public_id,
        actor=AuditActor(actor_type=actor_type, actor_id=actor_id),
        request_id=request_id,
        metadata=jsonable_encoder(metadata),
    )


def _queue_work_order_tag_notifications(
    db: Session,
    work_order: WorkOrder,
    *,
    previous_tags: tuple[str, ...],
    auth: dict[str, Any] | None,
) -> None:
    from app.services.staff_notifications import (
        StaffTagNotificationCommand,
        queue_staff_tag_notifications,
    )

    _actor_type, actor_id = _actor(auth)
    queue_staff_tag_notifications(
        db,
        StaffTagNotificationCommand(
            entity_kind="work order",
            entity_id=str(work_order.id),
            entity_reference=work_order.public_id,
            entity_title=work_order.title,
            target_url=f"/admin/dispatch/work-orders/{work_order.public_id}",
            current_tags=tuple(str(tag) for tag in (work_order.tags or ())),
            previous_tags=previous_tags,
            actor_person_id=actor_id,
        ),
    )


def _emit_work_order_created_event(
    db: Session, work_order: WorkOrder, *, actor: str | None
) -> None:
    emit_event(
        db,
        EventType.custom,
        {
            "name": "work_order.created",
            "tenant_id": str(OPERATOR_TENANT_ID),
            "work_order_id": str(work_order.id),
            "work_order_public_id": work_order.public_id,
            "project_id": str(work_order.project_id)
            if work_order.project_id is not None
            else None,
            "project_task_id": str(work_order.project_task_id)
            if work_order.project_task_id is not None
            else None,
            "status": work_order.status,
        },
        actor=actor,
        subscriber_id=work_order.subscriber_id,
    )


def _emit_work_order_updated_event(
    db: Session,
    work_order: WorkOrder,
    *,
    actor: str | None,
    changed_fields: tuple[str, ...],
) -> None:
    emit_event(
        db,
        EventType.custom,
        {
            "name": "work_order.updated",
            "tenant_id": str(OPERATOR_TENANT_ID),
            "work_order_id": str(work_order.id),
            "work_order_public_id": work_order.public_id,
            "project_id": str(work_order.project_id)
            if work_order.project_id is not None
            else None,
            "project_task_id": str(work_order.project_task_id)
            if work_order.project_task_id is not None
            else None,
            "status": work_order.status,
            "changed_fields": list(changed_fields),
        },
        actor=actor,
        subscriber_id=work_order.subscriber_id,
    )


def _validate_status(value: str) -> str:
    normalized = str(value or "").strip().lower()
    if normalized not in WORK_ORDER_STATUSES:
        raise HTTPException(
            status_code=422,
            detail=f"Unsupported work-order status: {normalized or value}",
        )
    return normalized


def _validate_queue_status(value: str) -> str:
    normalized = str(value or "").strip().lower()
    if normalized not in _QUEUE_STATUSES:
        raise WorkOrderCommandError(
            "invalid_assignment_queue_status",
            f"Unsupported assignment-queue status: {normalized or value}",
            kind="invalid",
        )
    return normalized


def _validate_schedule(
    start: datetime | None,
    end: datetime | None,
) -> None:
    if start is not None and end is not None and end <= start:
        raise HTTPException(
            status_code=422,
            detail="scheduled_end must be after scheduled_start",
        )


def _get_work_order(db: Session, public_id: str, *, lock: bool = False) -> WorkOrder:
    query = db.query(WorkOrder).filter(WorkOrder.public_id == str(public_id))
    if lock:
        query = query.with_for_update()
    row = query.one_or_none()
    if row is None:
        raise HTTPException(status_code=404, detail="Work order not found")
    return row


def _resolve_work_order(
    db: Session,
    payload: WorkOrderAssignmentQueueCreate,
    *,
    lock: bool,
) -> WorkOrder:
    query = db.query(WorkOrder)
    if payload.work_order_mirror_id is not None:
        query = query.filter(WorkOrder.id == coerce_uuid(payload.work_order_mirror_id))
    else:
        query = query.filter(WorkOrder.public_id == payload.crm_work_order_id)
    if lock:
        query = query.with_for_update()
    row = query.one_or_none()
    if row is None:
        raise WorkOrderCommandError(
            "work_order_not_found", "Work order not found", kind="not_found"
        )
    return row


def _get_technician(db: Session, technician_id: object) -> TechnicianProfile:
    try:
        row = db.get(TechnicianProfile, coerce_uuid(technician_id))
    except (TypeError, ValueError):
        row = None
    if row is None or not row.is_active:
        raise WorkOrderCommandError(
            "assignment_target_unavailable", "Technician not found", kind="not_found"
        )
    return row


def _get_rule(db: Session, rule_id: object) -> DispatchRule:
    try:
        row = db.get(DispatchRule, coerce_uuid(rule_id))
    except (TypeError, ValueError):
        row = None
    if row is None or not row.is_active:
        raise WorkOrderCommandError(
            "assignment_rule_unavailable", "Dispatch rule not found", kind="not_found"
        )
    return row


def _technician_name(db: Session, profile: TechnicianProfile) -> str:
    user = (
        db.get(SystemUser, profile.system_user_id)
        if profile.system_user_id is not None
        else None
    )
    if user is not None:
        name = user.display_name or f"{user.first_name} {user.last_name}".strip()
        if name:
            return name
    return profile.title or str(profile.person_id)


def _latest_queue_entry(
    db: Session,
    work_order_id: uuid.UUID,
) -> WorkOrderAssignmentQueue | None:
    return (
        db.query(WorkOrderAssignmentQueue)
        .populate_existing()
        .filter(WorkOrderAssignmentQueue.work_order_mirror_id == work_order_id)
        .order_by(
            WorkOrderAssignmentQueue.updated_at.desc(),
            WorkOrderAssignmentQueue.created_at.desc(),
            WorkOrderAssignmentQueue.id.desc(),
        )
        .first()
    )


def _same_queue_command(
    row: WorkOrderAssignmentQueue,
    data: dict[str, Any],
) -> bool:
    return all(
        getattr(row, key) == value
        for key, value in data.items()
        if key
        in {
            "status",
            "reason",
            "dispatch_rule_id",
            "assigned_technician_id",
            "assigned_vendor_id",
            "assigned_vendor_user_id",
        }
    )


def _emit_assignment_queue_event(
    db: Session,
    row: WorkOrder,
    entry: WorkOrderAssignmentQueue,
    context: CommandContext,
) -> None:
    emit_event(
        db,
        EventType.custom,
        {
            "name": "work_order.assignment_queue_transitioned",
            "tenant_id": str(OPERATOR_TENANT_ID),
            "work_order_id": str(row.id),
            "work_order_public_id": row.public_id,
            "queue_id": str(entry.id),
            "queue_status": entry.status,
            "command_id": str(context.command_id),
        },
        actor=context.actor,
        subscriber_id=row.subscriber_id,
    )


def _assignment_actor(context: CommandContext) -> AuditActor:
    identity = context.actor.removeprefix("user:")
    try:
        principal_id = uuid.UUID(identity)
    except ValueError:
        return AuditActor.system(context.actor)
    return AuditActor.user(str(principal_id))


def _assignment_audit(
    db: Session,
    *,
    action: str,
    work_order: WorkOrder,
    context: CommandContext,
    metadata: dict[str, object],
) -> None:
    stage_audit_event(
        db,
        action=action,
        entity_type="work_order",
        entity_id=work_order.public_id,
        actor=_assignment_actor(context),
        request_id=str(context.correlation_id),
        metadata=jsonable_encoder(metadata),
    )


def _assignment_work_order(
    db: Session, public_id: str, *, lock: bool = False
) -> WorkOrder:
    query = (
        db.query(WorkOrder).populate_existing().filter(WorkOrder.public_id == public_id)
    )
    if lock:
        query = query.with_for_update()
    row = query.one_or_none()
    if row is None:
        raise WorkOrderCommandError(
            "work_order_not_found", "Work order not found", kind="not_found"
        )
    return row


def _authorize_assignment(
    db: Session, *, access: PermissionAdmission, context: CommandContext
) -> None:
    if (
        access.principal_type != "system_user"
        or "operations:dispatch:assign" not in access.admitted_permissions
        or context.actor != str(access.principal_id)
    ):
        raise WorkOrderCommandError(
            "assignment_forbidden",
            "Staff dispatch permission admission is required",
            kind="forbidden",
        )
    actor = (
        db.query(SystemUser)
        .populate_existing()
        .filter(SystemUser.id == access.principal_id)
        .one_or_none()
    )
    if (
        actor is None
        or not actor.is_active
        or actor.user_type == "vendor"
        or db.query(FieldVendorUser.id)
        .filter(FieldVendorUser.system_user_id == actor.id)
        .first()
        is not None
    ):
        raise WorkOrderCommandError(
            "assignment_forbidden",
            "An active staff principal must dispatch work orders",
            kind="forbidden",
        )


def _vendor_selection(
    db: Session, target: VendorAssignmentTarget, *, lock: bool = False
) -> FieldVendorUser:
    """Revalidate native membership; caller locks its principal before the visit."""
    member = (
        db.query(FieldVendorUser)
        .populate_existing()
        .filter(FieldVendorUser.id == target.vendor_user_id)
        .one_or_none()
    )
    if member is None:
        raise WorkOrderCommandError(
            "assignment_target_unavailable",
            "Select an active attending vendor person",
            kind="invalid",
        )
    memberships = (
        db.query(FieldVendorUser)
        .populate_existing()
        .filter(FieldVendorUser.system_user_id == member.system_user_id)
        .order_by(FieldVendorUser.id)
    )
    rows = (memberships.with_for_update() if lock else memberships).all()
    active = [item for item in rows if item.is_active]
    user = (
        db.query(SystemUser)
        .populate_existing()
        .filter(SystemUser.id == member.system_user_id)
        .one_or_none()
    )
    if (
        len(active) != 1
        or active[0].id != member.id
        or user is None
        or not user.is_active
    ):
        raise WorkOrderCommandError(
            "assignment_target_unavailable",
            "Attending vendor person is inactive or ambiguous",
            kind="invalid",
        )
    vendors = db.query(Vendor).populate_existing().filter(Vendor.id == target.vendor_id)
    native = (vendors.with_for_update() if lock else vendors).one_or_none()
    bridges = (
        db.query(FieldVendor)
        .populate_existing()
        .filter(FieldVendor.id == member.vendor_id)
    )
    bridge = (bridges.with_for_update() if lock else bridges).one_or_none()
    if (
        native is None
        or not native.is_active
        or bridge is None
        or not bridge.is_active
        or bridge.native_vendor_id != native.id
    ):
        raise WorkOrderCommandError(
            "assignment_target_unavailable",
            "Attending person does not belong to the active native vendor",
            kind="invalid",
        )
    return member


def _assignment_principals(
    db: Session, row: WorkOrder | None, target: AssignmentTarget
) -> tuple[uuid.UUID, ...]:
    current = _latest_queue_entry(db, row.id) if row is not None else None
    member_ids = (
        {target.vendor_user_id} if isinstance(target, VendorAssignmentTarget) else set()
    )
    technician_ids: set[uuid.UUID | None] = (
        {target.technician_id}
        if isinstance(target, TechnicianAssignmentTarget)
        else set()
    )
    if current is not None and current.status == DispatchQueueStatus.assigned:
        member_ids.add(current.assigned_vendor_user_id)
        technician_ids.add(current.assigned_technician_id)
    users = {
        user_id
        for (user_id,) in db.query(FieldVendorUser.system_user_id)
        .filter(FieldVendorUser.id.in_(member_ids - {None}))
        .all()
    }
    users.update(
        user_id
        for (user_id,) in db.query(TechnicianProfile.system_user_id)
        .filter(TechnicianProfile.id.in_(technician_ids - {None}))
        .all()
        if user_id is not None
    )
    return tuple(sorted(users, key=str))


def _vendor_member_name(db: Session, member_id: uuid.UUID | None) -> str | None:
    if member_id is None:
        return None
    user = (
        db.query(SystemUser)
        .join(FieldVendorUser, FieldVendorUser.system_user_id == SystemUser.id)
        .filter(FieldVendorUser.id == member_id)
        .one_or_none()
    )
    return (
        (user.display_name or f"{user.first_name} {user.last_name}".strip())
        if user is not None
        else None
    )


def _assignment_preview(
    db: Session, row: WorkOrder, query: WorkOrderAssignmentQuery
) -> WorkOrderAssignmentPreview:
    if not row.is_active or row.status in TERMINAL_WORK_ORDER_STATUSES:
        raise WorkOrderCommandError(
            "work_order_not_assignable", "Work order is inactive or terminal"
        )
    technician_id = vendor_id = person_id = vendor_user_id = None
    technician_name = vendor_name = None
    if isinstance(query.target, TechnicianAssignmentTarget):
        profile = db.get(TechnicianProfile, query.target.technician_id)
        if profile is None or not profile.is_active:
            raise WorkOrderCommandError(
                "assignment_target_unavailable",
                "Technician is inactive or missing",
                kind="invalid",
            )
        technician_id, person_id = profile.id, profile.person_id
        technician_name = _technician_name(db, profile)
    elif isinstance(query.target, VendorAssignmentTarget):
        vendor = db.get(Vendor, query.target.vendor_id)
        if vendor is None or not vendor.is_active:
            raise WorkOrderCommandError(
                "assignment_target_unavailable",
                "Vendor is inactive or missing",
                kind="invalid",
            )
        member = _vendor_selection(db, query.target)
        vendor_id, vendor_name = vendor.id, vendor.name
        vendor_user_id = member.id
    else:
        raise WorkOrderCommandError(
            "invalid_assignment_target", "Unsupported assignment target", kind="invalid"
        )
    status = query.status
    if status not in {"scheduled", "dispatched"} | (
        {row.status} if row.status in {"in_progress", "paused"} else set()
    ):
        raise WorkOrderCommandError(
            "invalid_assignment_status", "Unsupported assignment status", kind="invalid"
        )
    if row.status in {"in_progress", "paused"}:
        # Assignment changes responsibility, never the execution lifecycle.
        status = row.status
    start = (
        query.scheduled_start
        if query.scheduled_start is not None
        else row.scheduled_start
    )
    end = query.scheduled_end if query.scheduled_end is not None else row.scheduled_end
    if start is not None and end is not None and end <= start:
        raise WorkOrderCommandError(
            "invalid_assignment_schedule",
            "Scheduled end must be after start",
            kind="invalid",
        )
    latest = _latest_queue_entry(db, row.id)
    if query.expected_revision is not None and _utc_revision(
        row.updated_at
    ) != _utc_revision(query.expected_revision):
        raise WorkOrderCommandError(
            "stale_assignment", "Work order changed since assignment preview"
        )
    current_generation = (
        latest.assignment_generation
        if latest is not None and latest.status == DispatchQueueStatus.assigned
        else None
    )
    if (
        query.expected_revision is not None
        or query.expected_assignment_generation is not None
    ) and current_generation != query.expected_assignment_generation:
        raise WorkOrderCommandError(
            "stale_assignment",
            "Current assignment generation changed; refresh the work order",
        )
    assigned = latest is not None and latest.status == DispatchQueueStatus.assigned
    previous_vendor = (
        db.get(Vendor, latest.assigned_vendor_id)
        if assigned and latest is not None and latest.assigned_vendor_id
        else None
    )
    return WorkOrderAssignmentPreview(
        work_order_id=row.public_id,
        revision=row.updated_at,
        previous=WorkOrderAssignmentState(
            row.status,
            latest.assigned_technician_id if assigned and latest is not None else None,
            latest.assigned_vendor_id if assigned and latest is not None else None,
            None,
            row.technician_name,
            previous_vendor.name if previous_vendor else None,
            row.scheduled_start,
            row.scheduled_end,
            latest.assigned_vendor_user_id if assigned and latest is not None else None,
            _vendor_member_name(db, latest.assigned_vendor_user_id)
            if assigned and latest is not None
            else None,
        ),
        result=WorkOrderAssignmentState(
            status,
            technician_id,
            vendor_id,
            person_id,
            technician_name,
            vendor_name,
            start,
            end,
            vendor_user_id,
            _vendor_member_name(db, vendor_user_id),
        ),
        assignment_generation=latest.assignment_generation
        if assigned and latest is not None
        else None,
    )


def _revoke_assignment(db: Session, row: WorkOrder) -> None:
    for previous in (
        db.query(WorkOrderAssignmentQueue)
        .filter(
            WorkOrderAssignmentQueue.work_order_mirror_id == row.id,
            WorkOrderAssignmentQueue.status == DispatchQueueStatus.assigned,
        )
        .all()
    ):
        previous.status = DispatchQueueStatus.skipped
        previous.assigned_technician_id = None
        previous.assigned_vendor_id = None
        previous.assigned_vendor_user_id = None
    row.assigned_to_crm_person_id = None
    row.assigned_to_name = None
    row.technician_name = None
    row.technician_phone = None
    if row.status == WorkOrderStatus.dispatched.value:
        row.status = WorkOrderStatus.scheduled.value
    db.flush()


def _receipt_outcome(receipt: WorkOrderAssignmentReceipt) -> WorkOrderAssignmentOutcome:
    data = receipt.outcome
    target = (
        VendorAssignmentTarget(
            uuid.UUID(data["vendor_id"]),
            uuid.UUID(data["vendor_user_id"]) if data.get("vendor_user_id") else None,
        )
        if data.get("vendor_id")
        else TechnicianAssignmentTarget(uuid.UUID(data["technician_id"]))
    )
    return WorkOrderAssignmentOutcome(
        uuid.UUID(data["queue_id"]),
        data["work_order_id"],
        target,
        data["status"],
        datetime.fromisoformat(data["revision"]),
        replayed=True,
        assignment_generation=uuid.UUID(data["assignment_generation"])
        if data.get("assignment_generation")
        else None,
    )


def _assign_work_order(
    db: Session,
    *,
    command: WorkOrderAssignmentCommand,
    context: CommandContext,
    preparation: WorkOrderAssignmentPreparation | None = None,
    expected_queue_id: uuid.UUID | None = None,
) -> WorkOrderAssignmentOutcome:
    if preparation is None:
        preparation = WorkOrderCommands.prepare_assignment(
            db,
            query=WorkOrderAssignmentPreparationQuery(
                work_order_public_id=command.work_order_public_id,
                target=command.target,
                access=command.access,
            ),
        )
    if (
        preparation.work_order_public_id != command.work_order_public_id
        or preparation.target != command.target
        or preparation.access != command.access
    ):
        raise WorkOrderCommandError(
            "stale_assignment", "Assignment preparation differs from command"
        )
    discovered, principals = preparation.current, preparation.principal_ids
    row = _assignment_work_order(db, command.work_order_public_id, lock=True)
    current = WorkOrderCommands.current_assignment(
        db, query=CurrentWorkOrderAssignmentQuery(work_order_id=row.id)
    )
    if (
        current != discovered
        or tuple(
            sorted(
                set(_assignment_principals(db, row, command.target))
                | {command.access.principal_id},
                key=str,
            )
        )
        != principals
    ):
        raise WorkOrderCommandError(
            "stale_assignment",
            "Attending selection changed while waiting; refresh the work order",
        )
    # Lock all discovered principals' membership rows together in UUID order,
    # before any native vendor/bridge row. Changed principal links fail stale.
    db.query(FieldVendorUser).populate_existing().filter(
        FieldVendorUser.system_user_id.in_(principals)
    ).order_by(FieldVendorUser.id).with_for_update().all()
    if (
        tuple(
            sorted(
                set(_assignment_principals(db, row, command.target))
                | {command.access.principal_id},
                key=str,
            )
        )
        != principals
    ):
        raise WorkOrderCommandError(
            "stale_assignment", "Attending principal changed; refresh the work order"
        )
    if expected_queue_id is not None:
        latest_requested = _latest_queue_entry(db, row.id)
        if latest_requested is None or latest_requested.id != expected_queue_id:
            raise WorkOrderCommandError(
                "stale_assignment", "Queue item is no longer current"
            )
    _authorize_assignment(db, access=command.access, context=context)
    fingerprint = _fingerprint(
        {
            "work_order": command.work_order_public_id,
            **(
                {"operation": command.operation.value}
                if command.operation != AssignmentOperation.assign
                else {}
            ),
            "target_kind": type(command.target).__name__,
            "target_id": str(
                command.target.vendor_id
                if isinstance(command.target, VendorAssignmentTarget)
                else command.target.technician_id
            ),
            "start": command.scheduled_start,
            "end": command.scheduled_end,
            "status": command.status,
            "reason": command.reason,
            "rule": command.dispatch_rule_id,
            "expected_revision": command.expected_revision,
            "expected_assignment_generation": command.expected_assignment_generation,
            "vendor_user_id": str(command.target.vendor_user_id)
            if isinstance(command.target, VendorAssignmentTarget)
            and command.target.vendor_user_id
            else None,
        }
    )
    receipt_query = db.query(WorkOrderAssignmentReceipt).filter(
        WorkOrderAssignmentReceipt.command_id == context.command_id
    )
    if context.idempotency_key:
        from sqlalchemy import or_

        receipt_query = db.query(WorkOrderAssignmentReceipt).filter(
            or_(
                WorkOrderAssignmentReceipt.command_id == context.command_id,
                WorkOrderAssignmentReceipt.idempotency_key == context.idempotency_key,
            )
        )
    receipts = receipt_query.all()
    if receipts:
        legacy_fingerprint = _fingerprint(
            {
                "work_order": command.work_order_public_id,
                **(
                    {"operation": command.operation.value}
                    if command.operation != AssignmentOperation.assign
                    else {}
                ),
                "target_kind": type(command.target).__name__,
                "target_id": str(
                    command.target.vendor_id
                    if isinstance(command.target, VendorAssignmentTarget)
                    else command.target.technician_id
                ),
                "start": command.scheduled_start,
                "end": command.scheduled_end,
                "status": command.status,
                "reason": command.reason,
                "rule": command.dispatch_rule_id,
                "expected_revision": command.expected_revision,
            }
        )
        historical = (
            len(receipts) == 1
            and "vendor_user_id" not in receipts[0].outcome
            and command.expected_assignment_generation is None
            and (
                not isinstance(command.target, VendorAssignmentTarget)
                or command.target.vendor_user_id is None
            )
            and receipts[0].fingerprint == legacy_fingerprint
        )
        if len(receipts) != 1 or (
            receipts[0].fingerprint != fingerprint and not historical
        ):
            raise WorkOrderCommandError(
                "assignment_idempotency_conflict",
                "Assignment command identity was reused with different content",
            )
        return _receipt_outcome(receipts[0])
    if command.expected_revision is not None and _utc_revision(
        row.updated_at
    ) != _utc_revision(command.expected_revision):
        raise WorkOrderCommandError(
            "stale_assignment", "Work order changed since assignment preview"
        )
    if isinstance(command.target, VendorAssignmentTarget) or (
        current is not None and current.vendor_id is not None
    ):
        if command.expected_revision is None:
            raise WorkOrderCommandError(
                "stale_assignment",
                "Expected work-order revision is required for vendor dispatch",
            )
        if (
            current is not None
            and current.generation != command.expected_assignment_generation
        ):
            raise WorkOrderCommandError(
                "stale_assignment",
                "Current assignment generation changed; refresh the work order",
            )
    if isinstance(command.target, VendorAssignmentTarget):
        if (
            command.operation == AssignmentOperation.reschedule
            and command.target.vendor_user_id is None
            and current is not None
        ):
            command = replace(
                command,
                target=VendorAssignmentTarget(
                    command.target.vendor_id, current.vendor_user_id
                ),
            )
        assert isinstance(command.target, VendorAssignmentTarget)
        member = _vendor_selection(db, command.target, lock=True)
        if member.system_user_id not in principals:
            raise WorkOrderCommandError(
                "stale_assignment",
                "Attending principal changed; refresh the work order",
            )
    preview = _assignment_preview(db, row, command)
    rule = None
    if command.dispatch_rule_id is not None:
        rule = db.get(DispatchRule, command.dispatch_rule_id)
        if rule is None or not rule.is_active:
            raise WorkOrderCommandError(
                "assignment_rule_unavailable",
                "Dispatch rule is inactive or missing",
                kind="invalid",
            )
    latest = _latest_queue_entry(db, row.id)
    target = preview.result
    if command.operation == AssignmentOperation.reschedule and (
        latest is None
        or latest.status != DispatchQueueStatus.assigned
        or latest.assigned_technician_id != target.technician_id
        or latest.assigned_vendor_id != target.vendor_id
        or latest.assigned_vendor_user_id != target.vendor_user_id
    ):
        raise WorkOrderCommandError(
            "reschedule_assignment_mismatch",
            "Schedule-only updates require the current assignment target",
        )
    if latest is None:
        latest = WorkOrderAssignmentQueue(work_order_mirror_id=row.id)
        db.add(latest)
    # Revoke historical assignments before setting the one canonical current row.
    for previous in (
        db.query(WorkOrderAssignmentQueue)
        .filter(
            WorkOrderAssignmentQueue.work_order_mirror_id == row.id,
            WorkOrderAssignmentQueue.status == DispatchQueueStatus.assigned,
            WorkOrderAssignmentQueue.id != latest.id,
        )
        .all()
    ):
        previous.status = DispatchQueueStatus.skipped
        previous.assigned_technician_id = previous.assigned_vendor_id = None
        previous.assigned_vendor_user_id = None
    db.flush()
    if command.operation == AssignmentOperation.assign:
        latest.assignment_generation = uuid.uuid4()
        latest.assignment_effective_at = datetime.now(UTC)
    latest.status = DispatchQueueStatus.assigned
    latest.assigned_technician_id = target.technician_id
    latest.assigned_vendor_id = target.vendor_id
    latest.assigned_vendor_user_id = target.vendor_user_id
    latest.reason = command.reason.strip() if command.reason else None
    latest.dispatch_rule_id = rule.id if rule else None
    profile = (
        db.get(TechnicianProfile, target.technician_id)
        if target.technician_id
        else None
    )
    row.assigned_to_crm_person_id = profile.crm_person_id if profile else None
    row.assigned_to_name = target.technician_name or target.vendor_name
    row.technician_name = target.technician_name
    row.technician_phone = None
    row.status = target.status
    row.scheduled_start, row.scheduled_end = (
        target.scheduled_start,
        target.scheduled_end,
    )
    mark_sub_authoritative(
        row,
        "assignment",
        details={
            "technician_id": str(target.technician_id)
            if target.technician_id
            else None,
            "vendor_id": str(target.vendor_id) if target.vendor_id else None,
            "vendor_user_id": str(target.vendor_user_id)
            if target.vendor_user_id
            else None,
            "status": row.status,
        },
    )
    db.flush()
    from dataclasses import asdict

    stage_audit_event(
        db,
        action="work_order.assigned",
        entity_type="work_order",
        entity_id=row.public_id,
        actor=_assignment_actor(context),
        request_id=str(context.correlation_id),
        metadata=jsonable_encoder(
            {
                "owner": "operations.work_order_commands",
                "command_id": str(context.command_id),
                "queue_id": str(latest.id),
                "assignment_generation": latest.assignment_generation,
                "previous": asdict(preview.previous),
                "result": asdict(target),
            }
        ),
    )
    emit_event(
        db,
        EventType.custom,
        {
            "name": "work_order.assigned",
            "tenant_id": str(OPERATOR_TENANT_ID),
            "work_order_id": str(row.id),
            "work_order_public_id": row.public_id,
            "queue_id": str(latest.id),
            "assignment_generation": str(latest.assignment_generation)
            if latest.assignment_generation
            else None,
            "command_id": str(context.command_id),
            "technician_id": str(target.technician_id)
            if target.technician_id
            else None,
            "vendor_id": str(target.vendor_id) if target.vendor_id else None,
            "vendor_user_id": str(target.vendor_user_id)
            if target.vendor_user_id
            else None,
            "status": row.status,
        },
        actor=context.actor,
        subscriber_id=row.subscriber_id,
    )
    from app.services.customer_experience_communications import (
        AssignmentCommunicationRequest,
        request_assignment_update,
    )

    request_assignment_update(
        db,
        request=AssignmentCommunicationRequest(
            subscriber_id=row.subscriber_id,
            work_order_id=row.id,
            work_order_public_id=row.public_id,
            title=row.title,
            command_id=context.command_id,
            queue_id=latest.id,
            preview=preview,
            project_id=row.project_id,
            project_task_id=row.project_task_id,
            ticket_id=row.origin_ticket_id,
        ),
    )
    outcome = WorkOrderAssignmentOutcome(
        latest.id,
        row.public_id,
        command.target,
        row.status,
        row.updated_at,
        assignment_generation=latest.assignment_generation,
    )
    db.add(
        WorkOrderAssignmentReceipt(
            command_id=context.command_id,
            idempotency_key=context.idempotency_key,
            fingerprint=fingerprint,
            outcome={
                "expected_revision": command.expected_revision.isoformat()
                if command.expected_revision
                else None,
                "expected_assignment_generation": str(
                    command.expected_assignment_generation
                )
                if command.expected_assignment_generation
                else None,
                "queue_id": str(latest.id),
                "assignment_generation": str(latest.assignment_generation)
                if latest.assignment_generation
                else None,
                "work_order_id": row.public_id,
                "technician_id": str(target.technician_id)
                if target.technician_id
                else None,
                "vendor_id": str(target.vendor_id) if target.vendor_id else None,
                "vendor_user_id": str(target.vendor_user_id)
                if target.vendor_user_id
                else None,
                "status": row.status,
                "revision": row.updated_at.isoformat(),
            },
        )
    )
    db.flush()
    return outcome


def _utc_revision(value: datetime) -> datetime:
    # PostgreSQL retains UTC awareness; the non-authoritative SQLite unit lane
    # loses timezone metadata on round-trip.
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


def _classification_eligibility(
    row: WorkOrder,
) -> tuple[ClassificationEligibility, str | None]:
    if not row.is_active:
        return ClassificationEligibility.INACTIVE, "This work order is inactive."
    if row.started_at is not None:
        return (
            ClassificationEligibility.EXECUTION_STARTED,
            "Field execution has already started.",
        )
    if row.status not in (WorkOrderStatus.draft.value, WorkOrderStatus.scheduled.value):
        return (
            ClassificationEligibility.STATUS_BLOCKED,
            "Select classification while work is draft or scheduled, before execution.",
        )
    return ClassificationEligibility.ALLOWED, None


def _classify_work_order(
    db: Session, *, command: ClassifyWorkOrderCommand
) -> WorkOrderClassificationOutcome:
    if (
        not isinstance(command.work_order_id, uuid.UUID)
        or not isinstance(command.classification, ExplicitWorkOrderClassification)
        or command.expected_revision.tzinfo is None
        or command.expected_revision.utcoffset() is None
    ):
        raise WorkOrderCommandError(
            "invalid_classification_command",
            "Explicit classification requires native identity and an aware revision",
            kind="invalid",
        )
    row = (
        db.query(WorkOrder)
        .filter(WorkOrder.id == command.work_order_id)
        .with_for_update()
        .one_or_none()
    )
    if row is None:
        raise WorkOrderCommandError(
            "work_order_not_found", "Work order not found", kind="not_found"
        )
    try:
        kind = WorkOrderKind(row.work_order_kind)
    except ValueError as exc:
        raise WorkOrderCommandError(
            "invalid_classification_kind",
            "Work-order kind requires repair",
            kind="invalid",
        ) from exc
    recommendation = recommend_template(
        query=WorkOrderTemplateQuery(kind=kind, classification=command.classification)
    )
    context = command.context
    request_key = context.idempotency_key or str(context.command_id)
    fingerprint = _fingerprint(
        {
            "work_order_id": str(command.work_order_id),
            "expected_revision": _utc_revision(command.expected_revision),
            "purpose": command.classification.purpose.value,
            "access_technology": command.classification.access_technology.value,
            "source": command.classification.source.value,
            "actor": context.actor,
        }
    )
    receipt = (
        db.query(AuditEvent)
        .filter(AuditEvent.action == "work_order.classified")
        .filter(AuditEvent.entity_type == "work_order")
        .filter(AuditEvent.entity_id == row.public_id)
        .filter(AuditEvent.request_id == request_key)
        .one_or_none()
    )
    if receipt is not None:
        metadata = receipt.metadata_ or {}
        if metadata.get("fingerprint") != fingerprint:
            raise WorkOrderCommandError(
                "classification_idempotency_conflict",
                "Classification identity was already used with different inputs",
            )
        recorded_result = metadata.get("result")
        if (
            not isinstance(recorded_result, dict)
            or type(recorded_result.get("template_version")) is not int
            or recorded_result["template_version"] != recommendation.identity.version
        ):
            raise WorkOrderCommandError(
                "classification_receipt_invalid",
                "Historical classification template version is unavailable for replay",
            )
        raw_revision = metadata.get("revision")
        if not isinstance(raw_revision, str):
            raise WorkOrderCommandError(
                "classification_receipt_invalid",
                "Classification receipt requires repair",
            )
        try:
            revision = datetime.fromisoformat(raw_revision)
        except ValueError as exc:
            raise WorkOrderCommandError(
                "classification_receipt_invalid",
                "Classification receipt requires repair",
            ) from exc
        if revision.tzinfo is None:
            raise WorkOrderCommandError(
                "classification_receipt_invalid",
                "Classification receipt requires repair",
            )
        return WorkOrderClassificationOutcome(
            work_order_id=row.id,
            public_id=row.public_id,
            revision=revision,
            recommendation=recommendation,
            replayed=True,
        )
    eligibility, blocked_reason = _classification_eligibility(row)
    if eligibility != ClassificationEligibility.ALLOWED:
        raise WorkOrderCommandError(
            "work_order_not_classifiable",
            blocked_reason or "Work order cannot be classified",
        )
    if _utc_revision(row.updated_at) != _utc_revision(command.expected_revision):
        raise WorkOrderCommandError(
            "stale_classification", "Work order changed; reload before classifying"
        )
    previous = {
        "purpose": row.purpose,
        "access_technology": row.access_technology,
        "template_version": row.template_version,
        "classification_source": row.classification_source,
    }
    row.purpose = command.classification.purpose.value
    row.access_technology = command.classification.access_technology.value
    row.classification_source = command.classification.source.value
    row.template_version = recommendation.identity.version
    row.updated_at = datetime.now(UTC)
    db.flush()
    revision = _utc_revision(row.updated_at)
    stage_audit_event(
        db,
        action="work_order.classified",
        entity_type="work_order",
        entity_id=row.public_id,
        actor=_assignment_actor(context),
        request_id=request_key,
        metadata={
            "owner": "operations.work_order_commands",
            "fingerprint": fingerprint,
            "revision": revision.isoformat(),
            "command_id": str(context.command_id),
            "previous": previous,
            "result": {
                "purpose": row.purpose,
                "access_technology": row.access_technology,
                "template_version": row.template_version,
                "classification_source": row.classification_source,
            },
            "reason": context.reason,
        },
    )
    event_payload = WorkOrderClassifiedEventPayload(
        work_order_id=row.id,
        work_order_public_id=row.public_id,
        purpose=command.classification.purpose,
        access_technology=command.classification.access_technology,
        classification_source=command.classification.source,
        template_version=recommendation.identity.version,
        revision=revision,
        command_id=context.command_id,
    )
    emit_event(
        db,
        EventType.work_order_classified,
        {
            "schema_version": event_payload.schema_version,
            "tenant_id": str(OPERATOR_TENANT_ID),
            "work_order_id": str(event_payload.work_order_id),
            "work_order_public_id": event_payload.work_order_public_id,
            "purpose": event_payload.purpose.value,
            "access_technology": event_payload.access_technology.value,
            "classification_source": event_payload.classification_source.value,
            "template_version": event_payload.template_version,
            "revision": event_payload.revision.isoformat(),
            "command_id": str(event_payload.command_id),
        },
        actor=context.actor,
        subscriber_id=row.subscriber_id,
    )
    return WorkOrderClassificationOutcome(
        work_order_id=row.id,
        public_id=row.public_id,
        revision=revision,
        recommendation=recommendation,
        replayed=False,
    )


class WorkOrderCommands:
    """Single native decision and transaction boundary for work-order commands."""

    @staticmethod
    def classification_snapshot(
        db: Session, *, query: WorkOrderClassificationQuery
    ) -> WorkOrderClassificationSnapshot:
        """Read native planning state and owner-derived eligibility, without writes."""
        row = db.get(WorkOrder, query.work_order_id)
        if row is None:
            raise WorkOrderCommandError(
                "work_order_not_found", "Work order not found", kind="not_found"
            )
        purpose, technology, source = (
            row.purpose,
            row.access_technology,
            row.classification_source,
        )
        if purpose is None:
            if any(
                value is not None
                for value in (technology, source, row.template_version)
            ):
                raise WorkOrderCommandError(
                    "classification_snapshot_invalid",
                    "Stored work-order classification requires repair",
                )
            classification = None
        elif technology is None or source is None or row.template_version is None:
            raise WorkOrderCommandError(
                "classification_snapshot_invalid",
                "Stored work-order classification requires repair",
            )
        else:
            try:
                classification = ExplicitWorkOrderClassification(
                    purpose=WorkOrderPurpose(purpose),
                    access_technology=WorkOrderAccessTechnology(technology),
                    source=ClassificationSource(source),
                )
            except (ValueError, WorkOrderTemplateError) as exc:
                raise WorkOrderCommandError(
                    "classification_snapshot_invalid",
                    "Stored work-order classification requires repair",
                ) from exc
        try:
            kind = WorkOrderKind(row.work_order_kind)
            status = WorkOrderStatus(row.status)
            recommendation = (
                recommend_template(
                    query=WorkOrderTemplateQuery(
                        kind=kind, classification=classification
                    )
                )
                if classification
                else None
            )
        except (ValueError, WorkOrderTemplateError) as exc:
            raise WorkOrderCommandError(
                "classification_snapshot_invalid",
                "Stored work-order classification requires repair",
            ) from exc
        if (
            recommendation is not None
            and row.template_version != recommendation.identity.version
        ):
            raise WorkOrderCommandError(
                "classification_snapshot_invalid",
                "Stored template version requires repair",
            )
        eligibility, reason = _classification_eligibility(row)
        purposes: list[WorkOrderPurpose] = []
        for purpose in WorkOrderPurpose:
            try:
                recommend_template(
                    query=WorkOrderTemplateQuery(
                        kind=kind,
                        classification=ExplicitWorkOrderClassification(
                            purpose=purpose,
                            access_technology=WorkOrderAccessTechnology.FIBER,
                        ),
                    )
                )
            except WorkOrderTemplateError as exc:
                if exc.code == TemplateErrorCode.INCOMPATIBLE_KIND:
                    continue
                raise
            purposes.append(purpose)
        return WorkOrderClassificationSnapshot(
            work_order_id=row.id,
            public_id=row.public_id,
            title=row.title,
            kind=kind,
            status=status,
            revision=_utc_revision(row.updated_at),
            eligibility=eligibility,
            blocked_reason=reason,
            classification=classification,
            recommendation=recommendation,
            purpose_options=tuple(purposes),
            technology_options=tuple(WorkOrderAccessTechnology),
        )

    @staticmethod
    def classify(
        db: Session, *, command: ClassifyWorkOrderCommand
    ) -> WorkOrderClassificationOutcome:
        """Explicitly select a governed scope before field execution begins."""
        return execute_owner_command(
            db,
            definition=OwnerCommandDefinition(
                owner="operations.work_order_commands",
                concern="explicit native work-order classification",
                name="classify",
            ),
            context=command.context,
            operation=lambda: _classify_work_order(db, command=command),
        )

    @staticmethod
    def current_assignment(
        db: Session, *, query: CurrentWorkOrderAssignmentQuery
    ) -> CurrentWorkOrderAssignment | None:
        row = _latest_queue_entry(db, query.work_order_id)
        if row is None or row.status != DispatchQueueStatus.assigned:
            return None
        return CurrentWorkOrderAssignment(
            queue_id=row.id,
            technician_id=row.assigned_technician_id,
            vendor_id=row.assigned_vendor_id,
            vendor_user_id=row.assigned_vendor_user_id,
            generation=row.assignment_generation,
            effective_at=row.assignment_effective_at,
        )

    @staticmethod
    def validate_subscriber_target(db: Session, subscriber_id: object) -> uuid.UUID:
        """Validate and normalize the subscriber target for a native job."""

        normalized = coerce_uuid(subscriber_id)
        if db.get(Subscriber, normalized) is None:
            raise HTTPException(status_code=404, detail="Subscriber not found")
        return normalized

    @staticmethod
    def validate_project_target(
        db: Session,
        project_id: object,
        *,
        subscriber_id: object,
    ) -> uuid.UUID:
        """Validate the native project binding owned by the work order."""

        normalized = coerce_uuid(project_id)
        project = db.get(Project, normalized)
        if project is None or not project.is_active:
            raise WorkOrderCommandError(
                "project_not_found", "Project not found", kind="not_found"
            )
        subscriber_uuid = coerce_uuid(subscriber_id)
        if project.subscriber_id is None:
            raise WorkOrderCommandError(
                "project_subscriber_missing",
                "Link a subscriber to the project before creating field work",
                kind="invalid",
            )
        if project.subscriber_id != subscriber_uuid:
            raise WorkOrderCommandError(
                "project_subscriber_mismatch",
                "Work order and project must belong to the same subscriber",
                kind="invalid",
            )
        return normalized

    @staticmethod
    def validate_project_task_target(
        db: Session,
        project_task_id: object,
        *,
        subscriber_id: object,
        project_id: object | None = None,
    ) -> tuple[uuid.UUID, uuid.UUID]:
        """Validate a native execution scope and return task/project ids."""

        normalized = coerce_uuid(project_task_id)
        task = db.get(ProjectTask, normalized)
        if task is None or not task.is_active:
            raise WorkOrderCommandError(
                "project_task_not_found", "Project task not found", kind="not_found"
            )
        if project_id is not None and coerce_uuid(project_id) != task.project_id:
            raise WorkOrderCommandError(
                "project_task_project_mismatch",
                "Project task does not belong to the selected project",
                kind="invalid",
            )
        WorkOrderCommands.validate_project_target(
            db,
            task.project_id,
            subscriber_id=subscriber_id,
        )
        return normalized, task.project_id

    @staticmethod
    def validate_native_subscriber(db: Session, *, subscriber_id: uuid.UUID) -> None:
        try:
            WorkOrderCommands.validate_subscriber_target(db, subscriber_id)
        except HTTPException as exc:
            raise WorkOrderCommandError(
                "native_create_invalid", str(exc.detail), kind="invalid"
            ) from exc

    @staticmethod
    def validate_native_assignment_target(
        db: Session, *, target: AssignmentTarget
    ) -> None:
        if isinstance(target, VendorAssignmentTarget):
            _vendor_selection(db, target)
        else:
            profile = (
                db.query(TechnicianProfile)
                .populate_existing()
                .filter(TechnicianProfile.id == target.technician_id)
                .one_or_none()
            )
            if profile is None or not profile.is_active:
                raise WorkOrderCommandError(
                    "assignment_target_unavailable",
                    "Technician is inactive or missing",
                    kind="invalid",
                )

    @staticmethod
    def native_work_order_snapshot(
        db: Session, *, work_order_id: uuid.UUID
    ) -> NativeWorkOrderSnapshot:
        """Current immutable canonical full header; receipts stay historical."""
        row = (
            db.query(WorkOrder)
            .populate_existing()
            .filter(WorkOrder.id == work_order_id)
            .one_or_none()
        )
        if row is None:
            raise WorkOrderCommandError(
                "work_order_not_found", "Native work order not found", kind="not_found"
            )
        return NativeWorkOrderSnapshot.model_validate(row)

    @staticmethod
    def assignment_queue_snapshot(
        db: Session, *, queue_id: uuid.UUID
    ) -> WorkOrderAssignmentQueueRead:
        row = (
            db.query(WorkOrderAssignmentQueue)
            .populate_existing()
            .filter(WorkOrderAssignmentQueue.id == queue_id)
            .one_or_none()
        )
        if row is None:
            raise WorkOrderCommandError(
                "assignment_queue_not_found", "Queue item not found", kind="not_found"
            )
        return WorkOrderAssignmentQueueRead.model_validate(row)

    @overload
    @staticmethod
    def stage_native_create(
        db: Session, *, command: NativeWorkOrderCreateCommand, context: CommandContext
    ) -> NativeWorkOrderCreated: ...

    @overload
    @staticmethod
    def stage_native_create(
        db: Session,
        *,
        command: ProjectNativeWorkOrderCreateCommand,
        context: CommandContext,
    ) -> ProjectNativeWorkOrderCreated: ...

    @staticmethod
    def stage_native_create(
        db: Session,
        *,
        command: NativeWorkOrderCreateCommand | ProjectNativeWorkOrderCreateCommand,
        context: CommandContext,
    ) -> NativeWorkOrderCreated | ProjectNativeWorkOrderCreated:
        """Flush-only shared writer; the registered coordinator owns completion."""
        if isinstance(command, ProjectNativeWorkOrderCreateCommand):
            return WorkOrderCommands._stage_project_native_create(
                db, command=command, context=context
            )
        from app.services.network.fiber_field_verification_job_scope import (
            PLAN_METADATA_KEY,
            serialize_fiber_plan_provenance,
        )

        if not db.in_transaction():
            raise WorkOrderCommandError(
                "native_create_transaction_required",
                "Native creation participant requires an owner transaction",
            )
        _authorize_assignment(db, access=command.access, context=context)
        if "operations:dispatch:write" not in command.access.admitted_permissions:
            raise WorkOrderCommandError(
                "assignment_forbidden",
                "Native creation permission admission is required",
                kind="forbidden",
            )
        existing = (
            db.query(WorkOrder.id)
            .filter(WorkOrder.public_id == command.public_id)
            .one_or_none()
        )
        try:
            row = WorkOrderCommands._create_native(
                db,
                WorkOrderHeaderCreate(
                    public_id=command.public_id,
                    subscriber_id=command.subscriber_id,
                    title=command.title,
                    description=command.description,
                    status="scheduled",
                    priority=command.priority,
                    work_type="survey",
                    address=command.address,
                    scheduled_start=command.scheduled_start,
                    scheduled_end=command.scheduled_end,
                    tags=["fiber", "field_verification"],
                ),
                auth={
                    "principal_id": str(command.access.principal_id),
                    "principal_type": command.access.principal_type,
                },
                request_id=command.request_id or str(context.correlation_id),
                idempotency_key=command.idempotency_key,
                owner_metadata={
                    PLAN_METADATA_KEY: serialize_fiber_plan_provenance(
                        command.provenance
                    )
                },
                commit=False,
            )
        except HTTPException as exc:
            raise WorkOrderCommandError(
                "native_create_invalid",
                str(exc.detail),
                kind="conflict" if exc.status_code == 409 else "invalid",
            ) from exc
        assert row.subscriber_id is not None
        return NativeWorkOrderCreated(
            row.id,
            row.public_id,
            row.updated_at,
            row.scheduled_start,
            row.scheduled_end,
            existing is not None,
            row.status,
            row.title,
            row.subscriber_id,
            row.priority,
        )

    @staticmethod
    def _stage_project_native_create(
        db: Session,
        *,
        command: ProjectNativeWorkOrderCreateCommand,
        context: CommandContext,
    ) -> ProjectNativeWorkOrderCreated:
        from app.services.field.work_order_kind import WorkOrderKind
        from app.services.owner_commands import (
            current_command_context,
            owner_command_active,
        )
        from app.services.staff_permission_admission import revalidate_staff_admission
        from app.services.work_order_templates import (
            WorkOrderTemplateQuery,
            recommend_template,
        )

        if not owner_command_active(db, owner="operations.project_work_order_handoff"):
            raise WorkOrderCommandError(
                "native_create_transaction_required", "Owner transaction required"
            )
        actor = revalidate_staff_admission(
            db,
            admission=command.access,
            permission="operations:dispatch:write",
            write=True,
        )
        if (
            context != current_command_context(db)
            or context.actor != str(actor.system_user_id)
            or actor.person_id != command.provenance.issuer_person_id
        ):
            raise WorkOrderCommandError(
                "assignment_forbidden", "Actor binding required", kind="forbidden"
            )
        provenance = command.provenance
        approval = provenance.approval
        recommendation = recommend_template(
            query=WorkOrderTemplateQuery(
                kind=WorkOrderKind.INFRASTRUCTURE,
                classification=provenance.classification,
            )
        )
        if (
            provenance.classification.access_technology.value
            != approval.content.technology.value
        ):
            raise WorkOrderCommandError(
                "native_create_invalid", "Approved technology mismatch"
            )
        if (
            not command.title.strip()
            or len(command.title) > 200
            or command.priority
            not in {
                "lower",
                "medium",
                "low",
                "normal",
                "high",
                "urgent",
            }
        ):
            raise WorkOrderCommandError(
                "native_create_invalid", "Title and governed priority required"
            )
        row = WorkOrder(
            public_id=command.public_id,
            title=command.title,
            description=command.description,
            priority=command.priority,
            status="draft",
            work_order_kind="infrastructure",
            subscriber_id=None,
            origin_ticket_id=None,
            project_id=approval.project_id,
            project_task_id=approval.task_id,
            purpose=provenance.classification.purpose.value,
            access_technology=provenance.classification.access_technology.value,
            classification_source=provenance.classification.source.value,
            template_version=recommendation.identity.version,
        )
        db.add(row)
        db.flush()
        return ProjectNativeWorkOrderCreated(
            row.id,
            row.public_id,
            row.updated_at,
            row.scheduled_start,
            row.scheduled_end,
            False,
            "draft",
            row.title,
            None,
            command.priority,
        )

    @staticmethod
    def create(
        db: Session,
        payload: WorkOrderHeaderCreate,
        *,
        auth: dict[str, Any] | None = None,
        request_id: str | None = None,
        idempotency_key: str | None = None,
        owner_metadata: dict[str, Any] | None = None,
        origin_ticket_id: object | None = None,
        commit: bool = True,
    ) -> WorkOrder:
        return WorkOrderCommands._create_native(
            db,
            payload,
            auth=auth,
            request_id=request_id,
            idempotency_key=idempotency_key,
            owner_metadata=owner_metadata,
            origin_ticket_id=origin_ticket_id,
            commit=commit,
        )

    @staticmethod
    def _create_native(
        db: Session,
        payload: WorkOrderHeaderCreate,
        *,
        auth: dict[str, Any] | None = None,
        request_id: str | None = None,
        idempotency_key: str | None = None,
        owner_metadata: dict[str, Any] | None = None,
        origin_ticket_id: object | None = None,
        commit: bool = True,
    ) -> WorkOrder:
        if origin_ticket_id is not None:
            lock_work_order_linkage(
                db, query=WorkOrderLinkageFence(ticket_id=coerce_uuid(origin_ticket_id))
            )
        data = _data(payload)
        requested_public_id = str(data.pop("public_id", None) or "").strip()
        if requested_public_id:
            public_id = requested_public_id
        elif idempotency_key:
            public_id = f"sub-{uuid.uuid5(_CREATE_ID_NAMESPACE, idempotency_key).hex}"
        else:
            public_id = f"sub-{uuid.uuid4().hex}"

        status = _validate_status(data.get("status") or WorkOrderStatus.draft.value)
        if status not in _INITIAL_STATUSES:
            raise HTTPException(
                status_code=422,
                detail="Native work orders must be created as draft or scheduled",
            )
        data["status"] = status
        if any(data.get(field) is not None for field in _ASSIGNMENT_HEADER_FIELDS):
            raise HTTPException(
                status_code=422,
                detail="Create the work order first, then use the assignment command",
            )
        _validate_schedule(data.get("scheduled_start"), data.get("scheduled_end"))

        data["subscriber_id"] = WorkOrderCommands.validate_subscriber_target(
            db, data["subscriber_id"]
        )
        if data.get("project_task_id") is not None:
            normalized_task_id, task_project_id = (
                WorkOrderCommands.validate_project_task_target(
                    db,
                    data["project_task_id"],
                    subscriber_id=data["subscriber_id"],
                    project_id=data.get("project_id"),
                )
            )
            data["project_task_id"] = normalized_task_id
            data["project_id"] = task_project_id
        elif data.get("project_id") is not None:
            data["project_id"] = WorkOrderCommands.validate_project_target(
                db,
                data["project_id"],
                subscriber_id=data["subscriber_id"],
            )
        normalized_origin_ticket_id: uuid.UUID | None = None
        if origin_ticket_id is not None:
            normalized_origin_ticket_id = coerce_uuid(origin_ticket_id)
            ticket = db.get(Ticket, normalized_origin_ticket_id)
            if ticket is None or not ticket.is_active:
                raise WorkOrderCommandError(
                    "origin_ticket_not_found",
                    "Origin ticket not found",
                    kind="not_found",
                )
            if ticket.subscriber_id != data["subscriber_id"]:
                raise WorkOrderCommandError(
                    "origin_ticket_subscriber_mismatch",
                    "Work order and origin ticket must belong to the same subscriber",
                    kind="invalid",
                )

        supplied_metadata = dict(data.pop("metadata_", None) or {})
        supplied_metadata.pop("fiber_field_verification_plan", None)
        owned_metadata = dict(owner_metadata or {})
        unsupported_owned_keys = set(owned_metadata) - {"fiber_field_verification_plan"}
        if unsupported_owned_keys:
            raise HTTPException(
                status_code=422,
                detail="Unsupported owner-managed work-order metadata",
            )
        supplied_metadata.update(owned_metadata)
        command_fingerprint = _fingerprint(
            {
                "public_id": public_id,
                **data,
                "origin_ticket_id": normalized_origin_ticket_id,
                "metadata": supplied_metadata,
            }
        )
        existing = (
            db.query(WorkOrder).filter(WorkOrder.public_id == public_id).one_or_none()
        )
        if existing is not None:
            existing_metadata = dict(existing.metadata_ or {})
            if (
                existing_metadata.get("native_create_fingerprint")
                == command_fingerprint
            ):
                return existing
            raise HTTPException(status_code=409, detail="Work order id already exists")

        metadata = supplied_metadata
        metadata["native_source"] = "sub"
        metadata["native_create_fingerprint"] = command_fingerprint
        row = WorkOrder(
            public_id=public_id,
            # Native work orders have no CRM provenance. Compatibility output
            # derives from public_id at the read boundary.
            metadata_=metadata,
            origin_ticket_id=normalized_origin_ticket_id,
            work_order_created_at=data.get("work_order_created_at"),
            **data,
        )
        db.add(row)
        try:
            db.flush()
            _audit(
                db,
                action="work_order.created",
                work_order=row,
                auth=auth,
                request_id=request_id or idempotency_key,
                metadata={
                    "owner": "operations.work_order_commands",
                    "result": {
                        "public_id": row.public_id,
                        "status": row.status,
                        "subscriber_id": str(row.subscriber_id),
                        "origin_ticket_id": str(row.origin_ticket_id)
                        if row.origin_ticket_id
                        else None,
                        "project_id": str(row.project_id) if row.project_id else None,
                        "project_task_id": str(row.project_task_id)
                        if row.project_task_id
                        else None,
                    },
                },
            )
            _emit_work_order_created_event(
                db,
                row,
                actor=_actor(auth)[1],
            )
            _queue_work_order_tag_notifications(
                db,
                row,
                previous_tags=(),
                auth=auth,
            )
            if commit:
                db.commit()
                db.refresh(row)
        except IntegrityError as exc:
            if not commit:
                # Participant callers do not own transaction completion. The
                # root owner rolls back and retries the whole command so its
                # state, provenance, and audit evidence remain atomic.
                raise
            # Concurrent replays converge through the public-id unique key.
            db.rollback()
            existing = (
                db.query(WorkOrder)
                .filter(WorkOrder.public_id == public_id)
                .one_or_none()
            )
            if (
                existing is not None
                and dict(existing.metadata_ or {}).get("native_create_fingerprint")
                == command_fingerprint
            ):
                return existing
            raise HTTPException(
                status_code=409,
                detail="Work order id already exists",
            ) from exc
        return row

    @staticmethod
    def stage_infrastructure_work_order(
        db: Session,
        payload: InfrastructureWorkOrderHeaderCreate,
        *,
        origin_ticket_id: object,
        auth: dict[str, Any] | None = None,
        request_id: str | None = None,
        idempotency_key: str,
    ) -> WorkOrder:
        """Stage an outage work order; the coordinator owns the commit."""

        lock_work_order_linkage(
            db, query=WorkOrderLinkageFence(ticket_id=coerce_uuid(origin_ticket_id))
        )
        data = _data(payload)
        status = _validate_status(data.get("status") or WorkOrderStatus.draft.value)
        if status not in _INITIAL_STATUSES:
            raise HTTPException(
                status_code=422,
                detail="Infrastructure work orders must start as draft or scheduled",
            )
        if any(data.get(field) is not None for field in _ASSIGNMENT_HEADER_FIELDS):
            raise HTTPException(
                status_code=422,
                detail="Create the work order first, then use the assignment command",
            )
        _validate_schedule(data.get("scheduled_start"), data.get("scheduled_end"))
        ticket_id = coerce_uuid(origin_ticket_id)
        ticket = db.get(Ticket, ticket_id)
        if ticket is None or not ticket.is_active:
            raise WorkOrderCommandError(
                "origin_ticket_not_found", "Origin ticket not found", kind="not_found"
            )
        data["status"] = status
        data["work_order_kind"] = "infrastructure"
        data["subscriber_id"] = None
        key = str(idempotency_key).strip()
        public_id = f"sub-{uuid.uuid5(_CREATE_ID_NAMESPACE, key).hex}"
        supplied_metadata = dict(data.pop("metadata_", None) or {})
        command_fingerprint = _fingerprint(
            {"public_id": public_id, **data, "origin_ticket_id": ticket_id}
        )
        existing = (
            db.query(WorkOrder).filter(WorkOrder.public_id == public_id).one_or_none()
        )
        if existing is not None:
            if (
                dict(existing.metadata_ or {}).get("native_create_fingerprint")
                == command_fingerprint
            ):
                return existing
            raise HTTPException(status_code=409, detail="Work order id already exists")
        supplied_metadata.update(
            {
                "native_source": "sub",
                "native_create_fingerprint": command_fingerprint,
                "infrastructure_origin_ticket_id": str(ticket_id),
            }
        )
        row = WorkOrder(
            public_id=public_id,
            origin_ticket_id=ticket_id,
            metadata_=supplied_metadata,
            work_order_created_at=data.get("work_order_created_at"),
            **data,
        )
        db.add(row)
        db.flush()
        _audit(
            db,
            action="work_order.created",
            work_order=row,
            auth=auth,
            request_id=request_id or key,
            metadata={
                "owner": "operations.work_order_commands",
                "work_order_kind": "infrastructure",
                "public_id": row.public_id,
                "origin_ticket_id": str(ticket_id),
            },
        )
        _emit_work_order_created_event(
            db,
            row,
            actor=_actor(auth)[1],
        )
        _queue_work_order_tag_notifications(db, row, previous_tags=(), auth=auth)
        return row

    @staticmethod
    def stage_automated_project_task_work_order(
        db: Session, command: AutomatedProjectTaskWorkOrderCommand
    ) -> WorkOrder:
        """Stage a configured template-task WorkOrder without committing."""

        try:
            return WorkOrderCommands.create(
                db,
                WorkOrderHeaderCreate(
                    title=command.title,
                    description=command.description,
                    subscriber_id=command.subscriber_id,
                    project_id=command.project_id,
                    project_task_id=command.project_task_id,
                    priority=command.priority or "normal",
                    work_type="install",
                    address=command.address,
                    requires_as_built_evidence=command.requires_as_built_evidence,
                ),
                request_id=command.idempotency_key,
                idempotency_key=command.idempotency_key,
                commit=False,
            )
        except HTTPException as exc:
            raise WorkOrderCommandError(
                "automated_project_task_rejected",
                str(exc.detail or "Configured ProjectTask WorkOrder was rejected"),
                kind="invalid" if exc.status_code == 422 else "conflict",
            ) from exc

    @staticmethod
    def update_header(
        db: Session,
        public_id: str,
        payload: WorkOrderHeaderUpdate,
        *,
        auth: dict[str, Any] | None = None,
        request_id: str | None = None,
        commit: bool = True,
    ) -> WorkOrder:
        row = _get_work_order(db, public_id, lock=True)
        previous_tags = tuple(str(tag) for tag in (row.tags or ()))
        data = _data(payload, exclude_unset=True)
        project_supplied = "project_id" in data
        project_task_supplied = "project_task_id" in data
        direct_assignment_fields = sorted(_ASSIGNMENT_HEADER_FIELDS.intersection(data))
        if direct_assignment_fields:
            raise HTTPException(
                status_code=422,
                detail=(
                    "Assignment fields are written by the assignment command: "
                    + ", ".join(direct_assignment_fields)
                ),
            )

        if "subscriber_id" in data:
            subscriber = db.get(Subscriber, coerce_uuid(data["subscriber_id"]))
            if subscriber is None:
                raise HTTPException(status_code=404, detail="Subscriber not found")
        if "project_id" in data:
            requested_project_id = data["project_id"]
            if requested_project_id is None and row.project_id is not None:
                raise WorkOrderCommandError(
                    "project_binding_immutable",
                    "A native work-order project binding cannot be removed",
                )
            if (
                row.project_id is not None
                and requested_project_id is not None
                and coerce_uuid(requested_project_id) != row.project_id
            ):
                raise WorkOrderCommandError(
                    "project_binding_immutable",
                    "A native work-order project binding cannot be changed",
                )
        if "project_task_id" in data:
            requested_task_id = data["project_task_id"]
            if requested_task_id is None and row.project_task_id is not None:
                raise WorkOrderCommandError(
                    "project_task_binding_immutable",
                    "A native work-order project-task binding cannot be removed",
                )
            if (
                row.project_task_id is not None
                and requested_task_id is not None
                and coerce_uuid(requested_task_id) != row.project_task_id
            ):
                raise WorkOrderCommandError(
                    "project_task_binding_immutable",
                    "A native work-order project-task binding cannot be changed",
                )
        if (
            "requires_as_built_evidence" in data
            and data["requires_as_built_evidence"] is None
        ):
            raise WorkOrderCommandError(
                "invalid_evidence_policy",
                "requires_as_built_evidence cannot be null",
                kind="invalid",
            )
        effective_project_id = data.get("project_id", row.project_id)
        effective_subscriber_id = data.get("subscriber_id", row.subscriber_id)
        effective_project_task_id = data.get("project_task_id", row.project_task_id)
        if effective_project_task_id is not None:
            normalized_task_id, task_project_id = (
                WorkOrderCommands.validate_project_task_target(
                    db,
                    effective_project_task_id,
                    subscriber_id=effective_subscriber_id,
                    project_id=effective_project_id,
                )
            )
            if project_task_supplied:
                data["project_task_id"] = normalized_task_id
            if effective_project_id is None:
                data["project_id"] = task_project_id
                effective_project_id = task_project_id
        if effective_project_id is not None:
            normalized_project_id = WorkOrderCommands.validate_project_target(
                db,
                effective_project_id,
                subscriber_id=effective_subscriber_id,
            )
            if project_supplied:
                data["project_id"] = normalized_project_id
        if "status" in data and data["status"] is not None:
            status = _validate_status(data["status"])
            if row.status in TERMINAL_WORK_ORDER_STATUSES and status != row.status:
                raise HTTPException(
                    status_code=409,
                    detail=f"Cannot reopen a work order in status {row.status}",
                )
            if status in _FIELD_EXECUTION_STATUSES and status != row.status:
                raise HTTPException(
                    status_code=422,
                    detail=(
                        f"Status {status} is written by the field transition owner"
                    ),
                )
            if status == WorkOrderStatus.dispatched.value and status != row.status:
                raise HTTPException(
                    status_code=422,
                    detail="Dispatched status is written by the assignment command",
                )
            if (
                row.status == WorkOrderStatus.dispatched.value
                and status in _INITIAL_STATUSES
            ):
                raise HTTPException(
                    status_code=422,
                    detail=(
                        "End an active assignment through its assignment-queue "
                        "transition"
                    ),
                )
            data["status"] = status

        _validate_schedule(
            data.get("scheduled_start", row.scheduled_start),
            data.get("scheduled_end", row.scheduled_end),
        )
        previous = {
            key: str(getattr(row, key)) if getattr(row, key) is not None else None
            for key in data
            if key != "metadata_"
        }
        if "metadata_" in data:
            metadata = dict(row.metadata_ or {})
            incoming = dict(data.pop("metadata_") or {})
            for reserved in (
                "native_source",
                "native_create_fingerprint",
                "native_field_source",
                "native_field_activity",
                "fiber_field_verification_plan",
            ):
                incoming.pop(reserved, None)
            metadata.update(incoming)
            metadata["native_source"] = "sub"
            row.metadata_ = metadata
        for key, value in data.items():
            setattr(row, key, value)
        mark_sub_authoritative(
            row, "work_order_update", details={"fields": sorted(data)}
        )
        _audit(
            db,
            action="work_order.updated",
            work_order=row,
            auth=auth,
            request_id=request_id,
            metadata={
                "owner": "operations.work_order_commands",
                "previous": previous,
                "result": {
                    key: str(getattr(row, key))
                    if getattr(row, key) is not None
                    else None
                    for key in data
                },
            },
        )
        _emit_work_order_updated_event(
            db,
            row,
            actor=_actor(auth)[1],
            changed_fields=tuple(sorted(data)),
        )
        _queue_work_order_tag_notifications(
            db,
            row,
            previous_tags=previous_tags,
            auth=auth,
        )
        if commit:
            db.commit()
            db.refresh(row)
        else:
            db.flush()
        return row

    @staticmethod
    def assignment_eligibility(
        db: Session,
        *,
        query: WorkOrderAssignmentEligibilityQuery,
    ) -> WorkOrderAssignmentEligibility:
        row = _assignment_work_order(db, query.work_order_public_id)
        if not row.is_active:
            return WorkOrderAssignmentEligibility(False, "Work order is inactive")
        if row.status in TERMINAL_WORK_ORDER_STATUSES:
            return WorkOrderAssignmentEligibility(
                False, f"Cannot assign a work order in status {row.status}"
            )
        return WorkOrderAssignmentEligibility(True)

    @staticmethod
    def assignment_replay_snapshot(
        db: Session, *, command_id: uuid.UUID
    ) -> WorkOrderAssignmentReplaySnapshot | None:
        """Coordinator provenance only; never an authorization or replay decision."""
        receipt = (
            db.query(WorkOrderAssignmentReceipt)
            .filter(WorkOrderAssignmentReceipt.command_id == command_id)
            .one_or_none()
        )
        if receipt is None:
            return None
        data = receipt.outcome
        return WorkOrderAssignmentReplaySnapshot(
            datetime.fromisoformat(data["expected_revision"])
            if data.get("expected_revision")
            else None,
            uuid.UUID(data["expected_assignment_generation"])
            if data.get("expected_assignment_generation")
            else None,
        )

    @staticmethod
    def prepare_assignment(
        db: Session, *, query: WorkOrderAssignmentPreparationQuery
    ) -> WorkOrderAssignmentPreparation:
        """Flush-only admission before a coordinator creates/locks its visit.

        Call inside the owning transaction, before native create/replay. The
        result cannot be reused in another transaction; the command rechecks
        exact current assignment and discovered principals after its visit lock.
        """
        row = (
            db.query(WorkOrder)
            .populate_existing()
            .filter(WorkOrder.public_id == query.work_order_public_id)
            .one_or_none()
        )
        current = (
            WorkOrderCommands.current_assignment(
                db, query=CurrentWorkOrderAssignmentQuery(work_order_id=row.id)
            )
            if row is not None
            else None
        )
        principals = tuple(
            sorted(
                set(_assignment_principals(db, row, query.target))
                | {query.access.principal_id},
                key=str,
            )
        )
        db.query(SystemUser).populate_existing().filter(
            SystemUser.id.in_(principals)
        ).order_by(SystemUser.id).with_for_update().all()
        technician_ids = (
            {query.target.technician_id}
            if isinstance(query.target, TechnicianAssignmentTarget)
            else set()
        )
        if current is not None and current.technician_id is not None:
            technician_ids.add(current.technician_id)
        if technician_ids:
            db.query(TechnicianProfile).populate_existing().filter(
                TechnicianProfile.id.in_(technician_ids)
            ).order_by(TechnicianProfile.id).with_for_update().all()
        return WorkOrderAssignmentPreparation(
            query.work_order_public_id, query.target, query.access, current, principals
        )

    @staticmethod
    def vendor_assignment_candidates(
        db: Session, *, query: VendorAssignmentCandidatesQuery
    ) -> VendorAssignmentCandidates:
        row = _assignment_work_order(db, query.work_order_public_id)
        eligibility = WorkOrderCommands.assignment_eligibility(
            db,
            query=WorkOrderAssignmentEligibilityQuery(
                work_order_public_id=row.public_id
            ),
        )
        if not eligibility.allowed:
            raise WorkOrderCommandError(
                "work_order_not_assignable",
                eligibility.reason or "Work order cannot be assigned",
            )
        vendor = (
            db.query(Vendor)
            .populate_existing()
            .filter(Vendor.id == query.vendor_id)
            .one_or_none()
        )
        if vendor is None or not vendor.is_active:
            raise WorkOrderCommandError(
                "assignment_target_unavailable",
                "Vendor is inactive or missing",
                kind="invalid",
            )
        members = (
            db.query(FieldVendorUser)
            .join(FieldVendor, FieldVendor.id == FieldVendorUser.vendor_id)
            .filter(
                FieldVendor.native_vendor_id == query.vendor_id,
                FieldVendorUser.is_active.is_(True),
            )
            .order_by(FieldVendorUser.id)
            .all()
        )
        candidates: list[VendorAssignmentCandidate] = []
        for member in members:
            try:
                _vendor_selection(
                    db, VendorAssignmentTarget(query.vendor_id, member.id)
                )
            except WorkOrderCommandError:
                continue
            user = db.get(SystemUser, member.system_user_id)
            assert user is not None
            candidates.append(
                VendorAssignmentCandidate(
                    member.id,
                    user.id,
                    user.display_name or f"{user.first_name} {user.last_name}".strip(),
                )
            )
        current = WorkOrderCommands.current_assignment(
            db, query=CurrentWorkOrderAssignmentQuery(work_order_id=row.id)
        )
        return VendorAssignmentCandidates(
            row.public_id,
            row.updated_at,
            current.generation if current else None,
            tuple(candidates),
            vendor.name,
        )

    @staticmethod
    def preview_assignment(
        db: Session,
        *,
        query: WorkOrderAssignmentQuery,
    ) -> WorkOrderAssignmentPreview:
        row = _assignment_work_order(db, query.work_order_public_id)
        return _assignment_preview(db, row, query)

    @staticmethod
    def assign(
        db: Session,
        *,
        command: WorkOrderAssignmentCommand,
        context: CommandContext,
    ) -> WorkOrderAssignmentOutcome:
        try:
            return execute_owner_command(
                db,
                definition=OwnerCommandDefinition(
                    owner="operations.work_order_commands",
                    concern="work-order assignment decisions and projection",
                    name="assign",
                ),
                context=context,
                operation=lambda: _assign_work_order(
                    db, command=command, context=context
                ),
            )
        except IntegrityError as exc:
            raise WorkOrderCommandError(
                "assignment_idempotency_conflict",
                "Concurrent assignment identity or target conflict",
            ) from exc

    @staticmethod
    def _stage_assignment(
        db: Session,
        *,
        command: WorkOrderAssignmentCommand,
        context: CommandContext,
        preparation: WorkOrderAssignmentPreparation,
    ) -> WorkOrderAssignmentOutcome:
        """Flush-only participant for the registered fiber verification coordinator."""
        if not db.in_transaction():
            raise WorkOrderCommandError(
                "assignment_transaction_required",
                "Assignment participant requires coordinator transaction",
            )
        return _assign_work_order(
            db, command=command, context=context, preparation=preparation
        )

    @staticmethod
    def create_queue_entry(
        db: Session,
        payload: WorkOrderAssignmentQueueCreate,
        *,
        context: CommandContext,
        access: PermissionAdmission,
    ) -> WorkOrderAssignmentQueueRead:
        return execute_owner_command(
            db,
            definition=OwnerCommandDefinition(
                owner="operations.work_order_commands",
                concern="work-order assignment-queue transitions",
                name="create_queue_entry",
            ),
            context=context,
            operation=lambda: WorkOrderAssignmentQueueRead.model_validate(
                WorkOrderCommands._create_queue_entry(
                    db, payload, context=context, access=access
                )
            ),
        )

    @staticmethod
    def _create_queue_entry(
        db: Session,
        payload: WorkOrderAssignmentQueueCreate,
        *,
        context: CommandContext,
        access: PermissionAdmission,
    ) -> WorkOrderAssignmentQueue:
        _authorize_assignment(db, access=access, context=context)
        row = _resolve_work_order(db, payload, lock=False)
        data = payload.model_dump()
        data.pop("expected_revision", None)
        data.pop("expected_assignment_generation", None)
        data.pop("work_order_mirror_id", None)
        data.pop("crm_work_order_id", None)
        data["status"] = _validate_queue_status(data.get("status") or "queued")
        data["reason"] = str(data.get("reason") or "").strip() or None
        if data.get("assigned_technician_id") and data.get("assigned_vendor_id"):
            raise WorkOrderCommandError(
                "invalid_assignment_target",
                "Exactly one assignment target is required",
                kind="invalid",
            )
        if data.get("dispatch_rule_id") is not None:
            data["dispatch_rule_id"] = _get_rule(db, data["dispatch_rule_id"]).id
        if data.get("assigned_technician_id") is not None:
            data["assigned_technician_id"] = _get_technician(
                db, data["assigned_technician_id"]
            ).id
        if (
            data["status"] == DispatchQueueStatus.assigned
            and data.get("assigned_technician_id") is None
            and data.get("assigned_vendor_id") is None
        ):
            raise WorkOrderCommandError(
                "invalid_assignment_target",
                "Assigned queue state requires exactly one target",
                kind="invalid",
            )
        if data["status"] == DispatchQueueStatus.assigned:
            target = (
                VendorAssignmentTarget(
                    data["assigned_vendor_id"], data.get("assigned_vendor_user_id")
                )
                if data.get("assigned_vendor_id")
                else TechnicianAssignmentTarget(data["assigned_technician_id"])
            )
            outcome = _assign_work_order(
                db,
                command=WorkOrderAssignmentCommand(
                    access=access,
                    work_order_public_id=row.public_id,
                    target=target,
                    reason=data["reason"],
                    dispatch_rule_id=data.get("dispatch_rule_id"),
                    expected_revision=payload.expected_revision,
                    expected_assignment_generation=payload.expected_assignment_generation,
                ),
                context=context,
            )
            db.flush()
            assigned_entry = db.get(WorkOrderAssignmentQueue, outcome.queue_id)
            assert assigned_entry is not None
            return assigned_entry

        db.query(SystemUser).populate_existing().filter(
            SystemUser.id == access.principal_id
        ).with_for_update().one_or_none()
        _authorize_assignment(db, access=access, context=context)
        row = _assignment_work_order(db, row.public_id, lock=True)
        latest = _latest_queue_entry(db, row.id)
        if latest is not None and _same_queue_command(latest, data):
            return latest
        _revoke_assignment(db, row)
        entry = WorkOrderAssignmentQueue(
            work_order_mirror_id=row.id,
            status=data["status"],
            reason=data.get("reason"),
            dispatch_rule_id=data.get("dispatch_rule_id"),
            assigned_technician_id=data.get("assigned_technician_id"),
            assigned_vendor_id=data.get("assigned_vendor_id"),
            assigned_vendor_user_id=data.get("assigned_vendor_user_id"),
        )
        db.add(entry)
        db.flush()
        _assignment_audit(
            db,
            action="work_order.assignment_queued",
            work_order=row,
            context=context,
            metadata={
                "owner": "operations.work_order_commands",
                "queue_id": str(entry.id),
                "result": {
                    "status": entry.status,
                    "technician_id": (
                        str(entry.assigned_technician_id)
                        if entry.assigned_technician_id
                        else None
                    ),
                    "dispatch_rule_id": (
                        str(entry.dispatch_rule_id) if entry.dispatch_rule_id else None
                    ),
                },
            },
        )
        _emit_assignment_queue_event(db, row, entry, context)
        db.flush()
        return entry

    @staticmethod
    def update_queue_entry(
        db: Session,
        queue_id: str,
        payload: WorkOrderAssignmentQueueUpdate,
        *,
        context: CommandContext,
        access: PermissionAdmission,
    ) -> WorkOrderAssignmentQueueRead:
        return execute_owner_command(
            db,
            definition=OwnerCommandDefinition(
                owner="operations.work_order_commands",
                concern="work-order assignment-queue transitions",
                name="update_queue_entry",
            ),
            context=context,
            operation=lambda: WorkOrderAssignmentQueueRead.model_validate(
                WorkOrderCommands._update_queue_entry(
                    db, queue_id, payload, context=context, access=access
                )
            ),
        )

    @staticmethod
    def _update_queue_entry(
        db: Session,
        queue_id: str,
        payload: WorkOrderAssignmentQueueUpdate,
        *,
        context: CommandContext,
        access: PermissionAdmission,
    ) -> WorkOrderAssignmentQueue:
        _authorize_assignment(db, access=access, context=context)
        try:
            queue_uuid = coerce_uuid(queue_id)
        except (TypeError, ValueError):
            queue_uuid = None
        entry = (
            db.query(WorkOrderAssignmentQueue)
            .filter(WorkOrderAssignmentQueue.id == queue_uuid)
            .one_or_none()
        )
        if entry is None:
            raise WorkOrderCommandError(
                "assignment_queue_not_found", "Queue item not found", kind="not_found"
            )
        data = payload.model_dump(exclude_unset=True)
        data.pop("expected_revision", None)
        data.pop("expected_assignment_generation", None)
        data.pop("expected_revision", None)
        data.pop("expected_assignment_generation", None)
        if data.get("status", entry.status) == DispatchQueueStatus.assigned:
            target_vendor = data.get("assigned_vendor_id", entry.assigned_vendor_id)
            target_technician = data.get(
                "assigned_technician_id", entry.assigned_technician_id
            )
            if data.get("assigned_vendor_id") is not None:
                target_technician = None
            if data.get("assigned_technician_id") is not None:
                target_vendor = None
            if (target_vendor is None) == (target_technician is None):
                raise WorkOrderCommandError(
                    "invalid_assignment_target",
                    "Exactly one assignment target is required",
                    kind="invalid",
                )
            if (
                target_vendor is None
                and data.get("assigned_vendor_user_id") is not None
            ):
                raise WorkOrderCommandError(
                    "invalid_assignment_target",
                    "Attending vendor person requires a vendor target",
                    kind="invalid",
                )
            order = db.get(WorkOrder, entry.work_order_mirror_id)
            assert order is not None
            target = (
                VendorAssignmentTarget(
                    target_vendor,
                    data.get("assigned_vendor_user_id", entry.assigned_vendor_user_id),
                )
                if target_vendor
                else TechnicianAssignmentTarget(uuid.UUID(str(target_technician)))
            )
            outcome = _assign_work_order(
                db,
                command=WorkOrderAssignmentCommand(
                    access=access,
                    work_order_public_id=order.public_id,
                    target=target,
                    expected_revision=payload.expected_revision,
                    expected_assignment_generation=payload.expected_assignment_generation,
                    reason=data.get("reason", entry.reason),
                    dispatch_rule_id=data.get(
                        "dispatch_rule_id", entry.dispatch_rule_id
                    ),
                ),
                context=context,
                expected_queue_id=entry.id,
            )
            assigned_entry = db.get(WorkOrderAssignmentQueue, outcome.queue_id)
            assert assigned_entry is not None
            return assigned_entry
        row = (
            db.query(WorkOrder)
            .populate_existing()
            .filter(WorkOrder.id == entry.work_order_mirror_id)
            .with_for_update()
            .one()
        )
        db.refresh(entry, with_for_update=True)
        latest_before = _latest_queue_entry(db, row.id)
        if latest_before is None or latest_before.id != entry.id:
            raise WorkOrderCommandError(
                "stale_assignment", "Queue item is no longer current"
            )
        previous_queue_status = entry.status
        previous_projection = {
            "status": row.status,
            "technician_id": (
                str(entry.assigned_technician_id)
                if entry.assigned_technician_id is not None
                else None
            ),
            "assigned_to_crm_person_id": row.assigned_to_crm_person_id,
            "assigned_to_name": row.assigned_to_name,
        }
        data = payload.model_dump(exclude_unset=True)
        data.pop("expected_revision", None)
        data.pop("expected_assignment_generation", None)
        if data.get("assigned_technician_id") and data.get("assigned_vendor_id"):
            raise WorkOrderCommandError(
                "invalid_assignment_target",
                "Exactly one assignment target is required",
                kind="invalid",
            )
        if "status" in data and data["status"] is not None:
            data["status"] = _validate_queue_status(data["status"])
        if "reason" in data:
            data["reason"] = str(data["reason"] or "").strip() or None
        if data.get("dispatch_rule_id") is not None:
            data["dispatch_rule_id"] = _get_rule(db, data["dispatch_rule_id"]).id
        if data.get("assigned_technician_id") is not None:
            data["assigned_technician_id"] = _get_technician(
                db, data["assigned_technician_id"]
            ).id
        resulting_status = data.get("status", entry.status)
        resulting_technician = data.get(
            "assigned_technician_id", entry.assigned_technician_id
        )
        resulting_vendor = data.get("assigned_vendor_id", entry.assigned_vendor_id)
        if (
            "assigned_technician_id" in data
            and data["assigned_technician_id"] is not None
        ):
            resulting_vendor = None
        if "assigned_vendor_id" in data and data["assigned_vendor_id"] is not None:
            resulting_technician = None
        if (
            resulting_status == DispatchQueueStatus.assigned
            and resulting_technician is None
            and resulting_vendor is None
        ):
            raise WorkOrderCommandError(
                "invalid_assignment_target",
                "Assigned queue state requires exactly one target",
                kind="invalid",
            )
        if _same_queue_command(entry, data):
            return entry
        if resulting_status == DispatchQueueStatus.assigned:
            target = (
                VendorAssignmentTarget(resulting_vendor)
                if resulting_vendor
                else TechnicianAssignmentTarget(uuid.UUID(str(resulting_technician)))
            )
            outcome = _assign_work_order(
                db,
                command=WorkOrderAssignmentCommand(
                    access=access,
                    work_order_public_id=row.public_id,
                    target=target,
                    reason=data.get("reason", entry.reason),
                    dispatch_rule_id=data.get(
                        "dispatch_rule_id", entry.dispatch_rule_id
                    ),
                ),
                context=context,
            )
            db.flush()
            assigned_entry = db.get(WorkOrderAssignmentQueue, outcome.queue_id)
            assert assigned_entry is not None
            return assigned_entry

        previous = {
            key: str(getattr(entry, key)) if getattr(entry, key) is not None else None
            for key in data
        }
        if "status" in data:
            entry.status = data["status"]
        if "reason" in data:
            entry.reason = data["reason"]
        if "dispatch_rule_id" in data:
            entry.dispatch_rule_id = data["dispatch_rule_id"]
        entry.assigned_vendor_id = None
        entry.assigned_vendor_user_id = None
        entry.assigned_technician_id = None
        removed_current_assignment = (
            latest_before is not None
            and latest_before.id == entry.id
            and previous_queue_status == DispatchQueueStatus.assigned
            and entry.status != DispatchQueueStatus.assigned
        )
        if removed_current_assignment:
            row.assigned_to_crm_person_id = None
            row.assigned_to_name = None
            row.technician_name = None
            row.technician_phone = None
            if row.status == WorkOrderStatus.dispatched.value:
                row.status = WorkOrderStatus.scheduled.value
            mark_sub_authoritative(
                row,
                "assignment_removed",
                details={
                    "queue_id": str(entry.id),
                    "queue_status": entry.status,
                },
            )
        db.flush()
        _assignment_audit(
            db,
            action="work_order.assignment_queue_transitioned",
            work_order=row,
            context=context,
            metadata={
                "owner": "operations.work_order_commands",
                "queue_id": str(entry.id),
                "previous": previous,
                "result": {
                    key: str(getattr(entry, key))
                    if getattr(entry, key) is not None
                    else None
                    for key in data
                },
                "work_order_projection": {
                    "previous": previous_projection,
                    "result": {
                        "status": row.status,
                        "technician_id": (
                            str(entry.assigned_technician_id)
                            if entry.status == DispatchQueueStatus.assigned
                            and entry.assigned_technician_id is not None
                            else None
                        ),
                        "assigned_to_crm_person_id": row.assigned_to_crm_person_id,
                        "assigned_to_name": row.assigned_to_name,
                    },
                },
            },
        )
        _emit_assignment_queue_event(db, row, entry, context)
        db.flush()
        return entry


work_order_commands = WorkOrderCommands()


def lock_work_order_linkage(db: Session, *, query: WorkOrderLinkageFence) -> None:
    """Canonical phantom fence: acquire before Ticket, origin or WorkOrder rows.

    Ticket issuers/closers share the Ticket key; standalone enrollment also holds
    its WorkOrder key. Reacquisition is safe inside one transaction.
    """
    from sqlalchemy import text

    if db.get_bind().dialect.name != "postgresql":
        return
    keys = ([f"ticket:{query.ticket_id}"] if query.ticket_id else []) + (
        [f"work_order:{query.work_order_id}"] if query.work_order_id else []
    )
    for key in keys:
        db.execute(
            text("SELECT pg_advisory_xact_lock(hashtextextended(:key, 0))"),
            {"key": "operations.work_order_commands:linkage:" + key},
        )


def stage_completion_enrollment_linkage(
    db: Session, *, query: CompletionEnrollmentLinkageQuery
) -> CompletionEnrollmentLinkage:
    """Admit an exact native enrollment before Project/WorkOrder row locks."""
    from app.services.owner_commands import owner_command_active

    if not owner_command_active(db, owner="operations.field_completion"):
        raise WorkOrderCommandError(
            "participant_required", "Completion owner required", kind="forbidden"
        )
    return _completion_enrollment_linkage(db, query=query)


def preview_completion_enrollment_linkage(
    db: Session, *, query: CompletionEnrollmentLinkagePreviewQuery
) -> CompletionEnrollmentLinkage:
    from app.services.staff_permission_admission import revalidate_staff_admission

    revalidate_staff_admission(
        db,
        admission=query.access,
        permission="operations:completion-review:write",
        write=True,
    )
    result = _completion_enrollment_linkage(db, query=query)
    revalidate_staff_admission(
        db,
        admission=query.access,
        permission="operations:completion-review:write",
        write=True,
    )
    return result


def _completion_enrollment_linkage(
    db: Session, *, query: CompletionEnrollmentLinkageQuery
) -> CompletionEnrollmentLinkage:
    lock_work_order_linkage(
        db,
        query=WorkOrderLinkageFence(
            ticket_id=query.origin_ticket_id, work_order_id=query.work_order_id
        ),
    )
    ticket: Ticket | None = None
    if query.origin_ticket_id is not None:
        ticket = db.scalar(
            select(Ticket)
            .where(Ticket.id == query.origin_ticket_id)
            .with_for_update()
            .execution_options(populate_existing=True)
        )
        if (
            ticket is None
            or not ticket.is_active
            or canonical_ticket_status_value(ticket.status) in {"closed", "canceled"}
        ):
            raise WorkOrderCommandError(
                "origin_ticket_terminal",
                "An active nonterminal origin ticket is required for enrollment",
                kind="invalid",
            )
    row = db.scalar(
        select(WorkOrder)
        .where(WorkOrder.id == query.work_order_id)
        .execution_options(populate_existing=True)
    )
    if row is None or row.origin_ticket_id != query.origin_ticket_id:
        raise WorkOrderCommandError(
            "scope_mismatch", "Work order linkage changed", kind="conflict"
        )
    if ticket is not None and ticket.subscriber_id != row.subscriber_id:
        raise WorkOrderCommandError(
            "origin_ticket_subscriber_mismatch",
            "Origin ticket subscriber changed",
            kind="invalid",
        )
    return CompletionEnrollmentLinkage(
        work_order_id=row.id,
        origin_ticket_id=row.origin_ticket_id,
        subscriber_id=row.subscriber_id,
        project_id=row.project_id,
        project_task_id=row.project_task_id,
    )
