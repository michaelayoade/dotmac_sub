"""Immutable values shared by fiber plan, evidence and adapter owners."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import TYPE_CHECKING
from uuid import UUID

if TYPE_CHECKING:
    from app.schemas.dispatch import WorkOrderAssignmentQueueRead
    from app.services.work_order_creation_contracts import NativeWorkOrderSnapshot

from app.services.owner_commands import CommandContext
from app.services.permission_admission import PermissionAdmission
from app.services.work_order_assignment_contracts import (
    AssignmentTarget,
    WorkOrderAssignmentOutcome,
)


@dataclass(frozen=True, slots=True)
class FiberWorkOrderReference:
    work_order_id: UUID
    work_order_public_id: str


@dataclass(frozen=True, slots=True)
class FiberFeatureEvidence:
    asset_type: str
    content_sha256: str
    current_work_orders: tuple[FiberWorkOrderReference, ...]
    display_name: str | None
    external_id: str | None
    geometry_sha256: str | None
    geometry_type: str | None
    needs_follow_up: bool
    priority: str
    row_sha256: str
    source_batch_id: UUID
    source_profile: str
    source_system: str
    staged_feature_id: UUID
    superseded_work_orders: tuple[FiberWorkOrderReference, ...]
    verification_state: str


@dataclass(frozen=True, slots=True)
class FiberSelectedEvidenceQuery:
    expected_worklist_report_sha256: str
    staged_feature_ids: tuple[UUID, ...]


@dataclass(frozen=True, slots=True)
class FiberSelectedEvidence:
    worklist_report_sha256: str
    selected_features: tuple[FiberFeatureEvidence, ...]


@dataclass(frozen=True, slots=True)
class FiberJobPlanScope:
    selected_features: tuple[FiberFeatureEvidence, ...]
    worklist_report_sha256: str
    scope_sha256: str
    schema_version: int = 1


@dataclass(frozen=True, slots=True)
class FiberVerificationPlanProvenance:
    plan_sha256: str
    scope: FiberJobPlanScope


@dataclass(frozen=True, slots=True, kw_only=True)
class FiberJobPlanQuery:
    expected_worklist_report_sha256: str
    staged_feature_ids: tuple[UUID, ...]
    subscriber_id: UUID
    title: str
    description: str | None
    priority: str
    address: str | None
    scheduled_start: datetime | None
    scheduled_end: datetime | None
    target: AssignmentTarget | None
    assignment_reason: str | None
    idempotency_key: str


@dataclass(frozen=True, slots=True)
class FiberPlanAuditPrincipal:
    principal_id: UUID
    request_id: str | None = None


@dataclass(frozen=True, slots=True, kw_only=True)
class FiberJobPlanCommand:
    query: FiberJobPlanQuery
    expected_plan_sha256: str
    context: CommandContext
    access: PermissionAdmission
    audit: FiberPlanAuditPrincipal


@dataclass(frozen=True, slots=True)
class FiberJobPlanPreview:
    query: FiberJobPlanQuery
    public_id: str
    scope: FiberJobPlanScope
    plan_sha256: str


@dataclass(frozen=True, slots=True)
class FiberJobPlanOutcome:
    plan: FiberJobPlanPreview
    replayed: bool
    work_order: NativeWorkOrderSnapshot
    assignment: WorkOrderAssignmentQueueRead | None
    assignment_receipt: WorkOrderAssignmentOutcome | None
