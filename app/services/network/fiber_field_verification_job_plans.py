"""Typed exact-source fiber job coordinator with one atomic owner boundary."""

from __future__ import annotations

import hashlib
import json
import uuid
from dataclasses import replace
from datetime import UTC, datetime

from sqlalchemy.orm import Session

from app.models.field_vendor import FieldVendorUser
from app.models.system_user import SystemUser
from app.services.audit_adapter import AuditActor, stage_audit_event
from app.services.domain_errors import DomainError
from app.services.network.fiber_field_verification_job_plan_contracts import (
    FiberJobPlanCommand,
    FiberJobPlanOutcome,
    FiberJobPlanPreview,
    FiberJobPlanQuery,
    FiberSelectedEvidenceQuery,
    FiberVerificationPlanProvenance,
)
from app.services.network.fiber_field_verification_job_scope import (
    build_planned_scope_metadata,
    planned_scope_payload,
)
from app.services.network.fiber_topology_field_worklist import (
    FiberTopologyFieldWorklistError,
    select_fiber_field_worklist_evidence,
)
from app.services.owner_commands import OwnerCommandDefinition, execute_owner_command
from app.services.work_order_assignment_contracts import (
    TechnicianAssignmentTarget,
    VendorAssignmentTarget,
    WorkOrderAssignmentCommand,
    WorkOrderAssignmentPreparationQuery,
)
from app.services.work_order_commands import work_order_commands
from app.services.work_order_creation_contracts import NativeWorkOrderCreateCommand

MAX_SELECTED_FEATURES = 100
_PUBLIC_ID_NAMESPACE = uuid.UUID("f00b5c75-0fda-4f92-a41a-6cc23d736af1")
_DEFINITION = OwnerCommandDefinition(
    owner="network.fiber_field_verification_jobs",
    concern="confirmed staged-source-to-native-job plan execution",
    name="execute_fiber_field_verification_job_plan",
)


class FiberFieldVerificationJobPlanError(DomainError):
    """Transport-neutral invalid, unavailable or stale plan evidence."""

    def __init__(self, detail: str, *, kind: str = "invalid_plan") -> None:
        super().__init__(
            code=f"network.fiber_field_verification_jobs.{kind}",
            message=detail,
            retryable=False,
        )


def _sha256(value: str, field: str) -> str:
    normalized = value.strip().lower()
    if len(normalized) != 64 or any(
        char not in "0123456789abcdef" for char in normalized
    ):
        raise FiberFieldVerificationJobPlanError(f"{field} must be a SHA-256 value")
    return normalized


def _digest(payload: object) -> str:
    return hashlib.sha256(
        json.dumps(
            payload, sort_keys=True, separators=(",", ":"), allow_nan=False
        ).encode()
    ).hexdigest()


def _time(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    if value.tzinfo is None or value.utcoffset() is None:
        value = value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


def _text(value: str | None) -> str | None:
    return value.strip() or None if value is not None else None


def normalize_fiber_job_plan_query(query: FiberJobPlanQuery) -> FiberJobPlanQuery:
    query = replace(
        query,
        expected_worklist_report_sha256=_sha256(
            query.expected_worklist_report_sha256, "expected_worklist_report_sha256"
        ),
        title=query.title.strip(),
        idempotency_key=query.idempotency_key.strip(),
        priority=query.priority.strip().lower(),
        description=_text(query.description),
        address=_text(query.address),
        assignment_reason=_text(query.assignment_reason),
        scheduled_start=_time(query.scheduled_start),
        scheduled_end=_time(query.scheduled_end),
    )
    if not query.title:
        raise FiberFieldVerificationJobPlanError("title is required")
    if not 16 <= len(query.idempotency_key) <= 160:
        raise FiberFieldVerificationJobPlanError(
            "idempotency_key must contain 16 to 160 characters"
        )
    if not 1 <= len(query.staged_feature_ids) <= MAX_SELECTED_FEATURES:
        raise FiberFieldVerificationJobPlanError(
            "select between 1 and 100 staged features"
        )
    if len(set(query.staged_feature_ids)) != len(query.staged_feature_ids):
        raise FiberFieldVerificationJobPlanError(
            "staged feature selection contains duplicates"
        )
    if query.priority not in {"lower", "low", "medium", "normal", "high", "urgent"}:
        raise FiberFieldVerificationJobPlanError("priority is unsupported")
    if (
        query.scheduled_start is not None
        and query.scheduled_end is not None
        and query.scheduled_end <= query.scheduled_start
    ):
        raise FiberFieldVerificationJobPlanError(
            "scheduled_end must be after scheduled_start"
        )
    if (
        isinstance(query.target, VendorAssignmentTarget)
        and query.target.vendor_user_id is None
    ):
        raise FiberFieldVerificationJobPlanError("select an attending vendor person")
    return query


def serialize_fiber_plan_command(preview: FiberJobPlanPreview) -> dict[str, object]:
    """Canonical reporting/persistence representation, preserving original plan hashes."""
    query = preview.query
    target = query.target
    result: dict[str, object] = {
        "address": query.address,
        "assigned_technician_id": str(target.technician_id)
        if isinstance(target, TechnicianAssignmentTarget)
        else None,
        "assigned_vendor_id": str(target.vendor_id)
        if isinstance(target, VendorAssignmentTarget)
        else None,
        "assignment_reason": query.assignment_reason,
        "description": query.description,
        "idempotency_key": query.idempotency_key,
        "priority": query.priority,
        "public_id": preview.public_id,
        "scheduled_end": query.scheduled_end.isoformat()
        if query.scheduled_end
        else None,
        "scheduled_start": query.scheduled_start.isoformat()
        if query.scheduled_start
        else None,
        "subscriber_id": str(query.subscriber_id),
        "title": query.title,
    }
    if isinstance(target, VendorAssignmentTarget):
        result["assigned_vendor_user_id"] = str(target.vendor_user_id)
    return result


def preview_fiber_field_verification_job_plan(
    db: Session, *, query: FiberJobPlanQuery
) -> FiberJobPlanPreview:
    """Read one consistent worklist and native target selection without writing."""
    query = normalize_fiber_job_plan_query(query)
    try:
        evidence = select_fiber_field_worklist_evidence(
            db,
            query=FiberSelectedEvidenceQuery(
                query.expected_worklist_report_sha256, query.staged_feature_ids
            ),
        )
    except FiberTopologyFieldWorklistError as exc:
        raise FiberFieldVerificationJobPlanError(
            str(exc), kind="stale_worklist"
        ) from exc
    work_order_commands.validate_native_subscriber(
        db, subscriber_id=query.subscriber_id
    )
    if query.target is not None:
        work_order_commands.validate_native_assignment_target(db, target=query.target)
    scope = build_planned_scope_metadata(
        selected_features=evidence.selected_features,
        worklist_report_sha256=evidence.worklist_report_sha256,
    )
    preview = FiberJobPlanPreview(
        query,
        f"sub-fv-{uuid.uuid5(_PUBLIC_ID_NAMESPACE, query.idempotency_key).hex}",
        scope,
        "",
    )
    return replace(
        preview,
        plan_sha256=_digest(
            {
                "command": serialize_fiber_plan_command(preview),
                **planned_scope_payload(scope),
            }
        ),
    )


def _authorize(db: Session, command: FiberJobPlanCommand) -> None:
    access = command.access
    required = {
        "operations:dispatch:assign",
        "operations:dispatch:write",
        "network:fiber:read",
    }
    if (
        access.principal_type != "system_user"
        or not required <= access.admitted_permissions
        or command.context.actor != str(access.principal_id)
        or command.audit.principal_id != access.principal_id
    ):
        raise FiberFieldVerificationJobPlanError(
            "Staff fiber dispatch permission admission is required", kind="forbidden"
        )
    principal = (
        db.query(SystemUser)
        .populate_existing()
        .filter(SystemUser.id == access.principal_id)
        .one_or_none()
    )
    if (
        principal is None
        or not principal.is_active
        or principal.user_type == "vendor"
        or db.query(FieldVendorUser.id)
        .filter(FieldVendorUser.system_user_id == access.principal_id)
        .first()
        is not None
    ):
        raise FiberFieldVerificationJobPlanError(
            "An active staff principal must execute fiber job plans", kind="forbidden"
        )


def execute_fiber_field_verification_job_plan(
    db: Session, *, command: FiberJobPlanCommand
) -> FiberJobPlanOutcome:
    """Revalidate and stage participants inside exactly one fresh owner transaction."""

    def operation() -> FiberJobPlanOutcome:
        # The physical snapshot must precede authorization, discovery and all SQL.
        if db.get_bind().dialect.name == "postgresql":
            db.connection(execution_options={"isolation_level": "REPEATABLE READ"})
        _authorize(db, command)
        preview = preview_fiber_field_verification_job_plan(db, query=command.query)
        if preview.plan_sha256 != _sha256(
            command.expected_plan_sha256, "expected_plan_sha256"
        ):
            raise FiberFieldVerificationJobPlanError(
                "fiber field-verification job plan changed; confirmation is stale",
                kind="stale_plan",
            )
        query = preview.query
        preparation = (
            work_order_commands.prepare_assignment(
                db,
                query=WorkOrderAssignmentPreparationQuery(
                    work_order_public_id=preview.public_id,
                    target=query.target,
                    access=command.access,
                ),
            )
            if query.target is not None
            else None
        )
        created = work_order_commands.stage_native_create(
            db,
            command=NativeWorkOrderCreateCommand(
                public_id=preview.public_id,
                subscriber_id=query.subscriber_id,
                title=query.title,
                description=query.description,
                priority=query.priority,
                address=query.address,
                scheduled_start=query.scheduled_start,
                scheduled_end=query.scheduled_end,
                idempotency_key=query.idempotency_key,
                provenance=FiberVerificationPlanProvenance(
                    preview.plan_sha256, preview.scope
                ),
                access=command.access,
                request_id=command.audit.request_id,
            ),
            context=command.context,
        )
        assignment = None
        if query.target is not None:
            assert preparation is not None
            assignment_command_id = uuid.uuid5(
                uuid.NAMESPACE_URL,
                f"fiber-verification-assignment:{query.idempotency_key}",
            )
            original = work_order_commands.assignment_replay_snapshot(
                db, command_id=assignment_command_id
            )
            assignment = work_order_commands._stage_assignment(
                db,
                command=WorkOrderAssignmentCommand(
                    work_order_public_id=created.public_id,
                    target=query.target,
                    access=command.access,
                    scheduled_start=created.scheduled_start,
                    scheduled_end=created.scheduled_end,
                    reason=query.assignment_reason or "fiber_field_verification_plan",
                    expected_revision=original.expected_revision
                    if original
                    else created.revision,
                    expected_assignment_generation=original.expected_assignment_generation
                    if original
                    else (
                        preparation.current.generation
                        if preparation and preparation.current
                        else None
                    ),
                ),
                context=replace(
                    command.context,
                    command_id=assignment_command_id,
                    idempotency_key=query.idempotency_key,
                ),
                preparation=preparation,
            )
        if not created.replayed:
            stage_audit_event(
                db,
                action="fiber_field_verification_job_plan.executed",
                entity_type="fiber_field_verification_job_plan",
                entity_id=preview.plan_sha256,
                actor=AuditActor.user(str(command.audit.principal_id)),
                request_id=command.audit.request_id,
                metadata={
                    "owner": _DEFINITION.owner,
                    "plan_sha256": preview.plan_sha256,
                    "public_id": created.public_id,
                    **planned_scope_payload(preview.scope),
                },
            )
        return FiberJobPlanOutcome(
            plan=preview,
            replayed=created.replayed,
            work_order=work_order_commands.native_work_order_snapshot(
                db, work_order_id=created.id
            ),
            assignment=work_order_commands.assignment_queue_snapshot(
                db, queue_id=assignment.queue_id
            )
            if assignment
            else None,
            assignment_receipt=assignment,
        )

    return execute_owner_command(
        db, definition=_DEFINITION, context=command.context, operation=operation
    )
