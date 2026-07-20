"""Exact staged-source plans for native fiber field-verification jobs.

This owner composes immutable field-verification worklist evidence into a bounded job
scope. It never writes ``WorkOrder`` or its assignment queue directly: execute
delegates those mutations to ``operations.work_order_commands`` in one caller-
owned transaction.
"""

from __future__ import annotations

import hashlib
import json
import uuid
from collections.abc import Sequence
from datetime import UTC, datetime
from typing import Any

from fastapi import HTTPException
from sqlalchemy.orm import Session

from app.models.audit import AuditActorType
from app.models.dispatch import TechnicianProfile, WorkOrderAssignmentQueue
from app.models.work_order import WorkOrder
from app.schemas.dispatch import WorkOrderHeaderCreate
from app.services.audit_adapter import stage_audit_event
from app.services.common import coerce_uuid
from app.services.network.fiber_field_verification_job_scope import (
    PLAN_METADATA_KEY,
    build_planned_scope_metadata,
)
from app.services.network.fiber_topology_field_worklist import (
    FiberTopologyFieldWorklistReport,
    reconcile_fiber_field_worklist,
)
from app.services.work_order_commands import work_order_commands

MAX_SELECTED_FEATURES = 100
_PUBLIC_ID_NAMESPACE = uuid.UUID("f00b5c75-0fda-4f92-a41a-6cc23d736af1")
_SHA256_HEX = frozenset("0123456789abcdef")
_WORK_ORDER_PRIORITIES = frozenset(
    {"lower", "low", "medium", "normal", "high", "urgent"}
)


class FiberFieldVerificationJobPlanError(ValueError):
    """Raised when exact job-plan evidence or confirmation is invalid."""

    def __init__(self, detail: str, *, status_code: int = 422) -> None:
        super().__init__(detail)
        self.detail = detail
        self.status_code = status_code


def _sha256(value: object, field: str) -> str:
    normalized = str(value or "").strip().lower()
    if len(normalized) != 64 or any(char not in _SHA256_HEX for char in normalized):
        raise FiberFieldVerificationJobPlanError(f"{field} must be a SHA-256 value")
    return normalized


def _digest(payload: object) -> str:
    return hashlib.sha256(
        json.dumps(
            payload,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode()
    ).hexdigest()


def _timestamp(value: datetime | None) -> str | None:
    if value is None:
        return None
    if value.tzinfo is None or value.utcoffset() is None:
        value = value.replace(tzinfo=UTC)
    return value.astimezone(UTC).isoformat()


def _uuid(value: object, field: str) -> uuid.UUID:
    try:
        return coerce_uuid(value)
    except (TypeError, ValueError) as exc:
        raise FiberFieldVerificationJobPlanError(f"{field} is invalid") from exc


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


def _public_id(idempotency_key: str) -> str:
    return f"sub-fv-{uuid.uuid5(_PUBLIC_ID_NAMESPACE, idempotency_key).hex}"


def _selected_rows(
    report: FiberTopologyFieldWorklistReport,
    staged_feature_ids: Sequence[object],
) -> list[dict[str, object]]:
    if not staged_feature_ids:
        raise FiberFieldVerificationJobPlanError(
            "at least one staged feature must be selected"
        )
    if len(staged_feature_ids) > MAX_SELECTED_FEATURES:
        raise FiberFieldVerificationJobPlanError(
            f"at most {MAX_SELECTED_FEATURES} staged features may be selected"
        )
    selected_ids = [_uuid(value, "staged_feature_id") for value in staged_feature_ids]
    if len(set(selected_ids)) != len(selected_ids):
        raise FiberFieldVerificationJobPlanError(
            "staged feature selection contains duplicates"
        )
    requested = {str(value) for value in selected_ids}
    rows = [row for row in report.rows if str(row["staged_feature_id"]) in requested]
    found = {str(row["staged_feature_id"]) for row in rows}
    missing = sorted(requested - found)
    if missing:
        raise FiberFieldVerificationJobPlanError(
            "selected staged features are not current worklist rows: "
            + ", ".join(missing),
            status_code=409,
        )
    rows.sort(
        key=lambda row: (
            str(row["source_system"]),
            str(row["asset_type"]),
            str(row["external_id"]),
            str(row["source_profile"]),
            str(row["staged_feature_id"]),
        )
    )
    return rows


def _feature_scope(row: dict[str, object]) -> dict[str, object]:
    return {
        "asset_type": row["asset_type"],
        "content_sha256": row["content_sha256"],
        "current_work_orders": row["current_work_orders"],
        "display_name": row["display_name"],
        "external_id": row["external_id"],
        "geometry_sha256": row["geometry_sha256"],
        "geometry_type": row["geometry_type"],
        "needs_follow_up": row["needs_follow_up"],
        "priority": row["priority"],
        "row_sha256": row["row_sha256"],
        "source_batch_id": row["source_batch_id"],
        "source_profile": row["source_profile"],
        "source_system": row["source_system"],
        "staged_feature_id": row["staged_feature_id"],
        "superseded_work_orders": row["superseded_work_orders"],
        "verification_state": row["verification_state"],
    }


def preview_fiber_field_verification_job_plan(
    db: Session,
    *,
    expected_worklist_report_sha256: object,
    staged_feature_ids: Sequence[object],
    subscriber_id: object,
    title: str,
    description: str | None,
    priority: str,
    address: str | None,
    scheduled_start: datetime | None,
    scheduled_end: datetime | None,
    assigned_technician_id: object | None,
    assignment_reason: str | None,
    idempotency_key: str,
) -> dict[str, object]:
    """Return a write-free exact plan over current worklist evidence."""

    expected_report = _sha256(
        expected_worklist_report_sha256, "expected_worklist_report_sha256"
    )
    normalized_title = str(title or "").strip()
    normalized_key = str(idempotency_key or "").strip()
    if not normalized_title:
        raise FiberFieldVerificationJobPlanError("title is required")
    if len(normalized_key) < 16 or len(normalized_key) > 160:
        raise FiberFieldVerificationJobPlanError(
            "idempotency_key must contain 16 to 160 characters"
        )
    if (
        scheduled_start is not None
        and scheduled_end is not None
        and scheduled_end <= scheduled_start
    ):
        raise FiberFieldVerificationJobPlanError(
            "scheduled_end must be after scheduled_start"
        )
    subscriber_uuid = _uuid(subscriber_id, "subscriber_id")
    normalized_priority = str(priority).strip().lower()
    if normalized_priority not in _WORK_ORDER_PRIORITIES:
        raise FiberFieldVerificationJobPlanError("priority is unsupported")

    # The worklist owner must open the repeatable snapshot before any SQL read.
    report = reconcile_fiber_field_worklist(db)
    if report.report_sha256 != expected_report:
        raise FiberFieldVerificationJobPlanError(
            "field-verification worklist changed; generate a fresh preview",
            status_code=409,
        )
    try:
        work_order_commands.validate_subscriber_target(db, subscriber_uuid)
    except HTTPException as exc:
        raise FiberFieldVerificationJobPlanError(
            str(exc.detail), status_code=exc.status_code
        ) from exc
    technician_uuid: uuid.UUID | None = None
    if assigned_technician_id is not None:
        technician_uuid = _uuid(assigned_technician_id, "assigned_technician_id")
        technician = db.get(TechnicianProfile, technician_uuid)
        if technician is None or not technician.is_active:
            raise FiberFieldVerificationJobPlanError(
                "active technician not found", status_code=404
            )

    selected_features = [
        _feature_scope(row) for row in _selected_rows(report, staged_feature_ids)
    ]
    command = {
        "address": str(address or "").strip() or None,
        "assigned_technician_id": (
            str(technician_uuid) if technician_uuid is not None else None
        ),
        "assignment_reason": str(assignment_reason or "").strip() or None,
        "description": str(description or "").strip() or None,
        "idempotency_key": normalized_key,
        "priority": normalized_priority,
        "public_id": _public_id(normalized_key),
        "scheduled_end": _timestamp(scheduled_end),
        "scheduled_start": _timestamp(scheduled_start),
        "subscriber_id": str(subscriber_uuid),
        "title": normalized_title,
    }
    scope_metadata = build_planned_scope_metadata(
        selected_features=selected_features,
        worklist_report_sha256=report.report_sha256,
    )
    plan_payload: dict[str, object] = {
        "command": command,
        **scope_metadata,
    }
    plan_sha256 = _digest(plan_payload)
    return {
        **plan_payload,
        "plan_sha256": plan_sha256,
    }


def execute_fiber_field_verification_job_plan(
    db: Session,
    *,
    expected_plan_sha256: object,
    auth: dict[str, Any] | None = None,
    request_id: str | None = None,
    **preview_args: Any,
) -> dict[str, object]:
    """Revalidate and atomically create/assign the exact previewed native job."""

    expected_plan = _sha256(expected_plan_sha256, "expected_plan_sha256")
    preview = preview_fiber_field_verification_job_plan(db, **preview_args)
    if preview["plan_sha256"] != expected_plan:
        raise FiberFieldVerificationJobPlanError(
            "fiber field-verification job plan changed; confirmation is stale",
            status_code=409,
        )
    command = preview["command"]
    assert isinstance(command, dict)
    public_id = str(command["public_id"])
    existing_before = (
        db.query(WorkOrder).filter(WorkOrder.public_id == public_id).one_or_none()
    )
    metadata = {
        PLAN_METADATA_KEY: {
            "plan_sha256": preview["plan_sha256"],
            **{
                key: preview[key]
                for key in (
                    "schema_version",
                    "scope_sha256",
                    "selected_feature_count",
                    "selected_features",
                    "worklist_report_sha256",
                )
            },
        }
    }
    try:
        work_order = work_order_commands.create(
            db,
            WorkOrderHeaderCreate(
                public_id=public_id,
                subscriber_id=_uuid(command["subscriber_id"], "subscriber_id"),
                title=str(command["title"]),
                description=(
                    str(command["description"])
                    if command.get("description") is not None
                    else None
                ),
                status="scheduled",
                priority=str(command["priority"]),
                work_type="survey",
                address=(
                    str(command["address"])
                    if command.get("address") is not None
                    else None
                ),
                scheduled_start=(
                    datetime.fromisoformat(str(command["scheduled_start"]))
                    if command.get("scheduled_start") is not None
                    else None
                ),
                scheduled_end=(
                    datetime.fromisoformat(str(command["scheduled_end"]))
                    if command.get("scheduled_end") is not None
                    else None
                ),
                tags=["fiber", "field_verification"],
            ),
            auth=auth,
            request_id=request_id,
            idempotency_key=str(command["idempotency_key"]),
            owner_metadata=metadata,
            commit=False,
        )
        assignment: WorkOrderAssignmentQueue | None = None
        if command.get("assigned_technician_id") is not None:
            assignment = work_order_commands.assign(
                db,
                work_order.public_id,
                technician_id=command["assigned_technician_id"],
                scheduled_start=work_order.scheduled_start,
                scheduled_end=work_order.scheduled_end,
                reason=(
                    str(command["assignment_reason"])
                    if command.get("assignment_reason") is not None
                    else "fiber_field_verification_plan"
                ),
                auth=auth,
                request_id=request_id,
                commit=False,
            )
        replayed = existing_before is not None
        if not replayed:
            actor_type, actor_id = _actor(auth)
            stage_audit_event(
                db,
                action="fiber_field_verification_job_plan.executed",
                entity_type="fiber_field_verification_job_plan",
                entity_id=str(preview["plan_sha256"]),
                actor_type=actor_type,
                actor_id=actor_id,
                request_id=request_id,
                metadata={
                    "owner": "network.fiber_field_verification_jobs",
                    "plan_sha256": preview["plan_sha256"],
                    "public_id": work_order.public_id,
                    "selected_feature_count": preview["selected_feature_count"],
                    "selected_features": preview["selected_features"],
                    "worklist_report_sha256": preview["worklist_report_sha256"],
                },
            )
        db.commit()
        db.refresh(work_order)
        if assignment is not None:
            db.refresh(assignment)
        return {
            "assignment": assignment,
            "plan": preview,
            "replayed": replayed,
            "work_order": work_order,
        }
    except (FiberFieldVerificationJobPlanError, HTTPException):
        db.rollback()
        raise
    except Exception:
        db.rollback()
        raise


__all__ = [
    "MAX_SELECTED_FEATURES",
    "FiberFieldVerificationJobPlanError",
    "execute_fiber_field_verification_job_plan",
    "preview_fiber_field_verification_job_plan",
]
