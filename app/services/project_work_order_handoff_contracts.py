"""Explicit native project infrastructure issuance contracts."""

from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from uuid import UUID

from app.schemas.infrastructure import InfrastructureReference
from app.services.owner_commands import CommandContext
from app.services.permission_admission import PermissionAdmission
from app.services.project_engineering_plan_contracts import (
    EngineeringTechnology,
    PlanLocation,
)
from app.services.work_order_creation_values import WorkOrderCreationPriority
from app.services.work_order_templates import (
    ExplicitWorkOrderClassification,
    WorkOrderPurpose,
)

OWNER = "operations.project_work_order_handoff"
CONCERN = "approved project infrastructure work-order issuance"
PERMISSION = "operations:dispatch:write"


@dataclass(frozen=True, slots=True, kw_only=True)
class IssueProjectWorkOrder:
    context: CommandContext
    access: PermissionAdmission
    project_id: UUID
    task_id: UUID | None
    expected_project_updated_at: datetime
    expected_task_updated_at: datetime | None
    plan_id: UUID
    revision_id: UUID
    approval_id: UUID
    digest: str
    infrastructure: InfrastructureReference
    location: PlanLocation
    classification: ExplicitWorkOrderClassification
    title: str
    description: str | None
    priority: WorkOrderCreationPriority


@dataclass(frozen=True, slots=True, kw_only=True)
class ProjectWorkOrderIssued:
    issuance_id: UUID
    work_order_id: UUID
    public_id: str
    project_id: UUID
    task_id: UUID | None
    plan_id: UUID
    revision_id: UUID
    approval_id: UUID
    digest: str
    command_id: UUID
    issued_at: datetime
    replayed: bool = False


@dataclass(frozen=True, slots=True, kw_only=True)
class ProjectWorkOrderIssuedEvent:
    issuance_id: UUID
    work_order_id: UUID
    project_id: UUID
    task_id: UUID | None
    plan_id: UUID
    revision_id: UUID
    approval_id: UUID
    digest: str
    command_id: UUID
    correlation_id: UUID
    reason: str
    schema_version: int = 1


class CompletionIssuancePurpose(StrEnum):
    read = "read"
    review = "review"


@dataclass(frozen=True, slots=True, kw_only=True)
class CompletionIssuanceEvidenceQuery:
    access: PermissionAdmission
    purpose: CompletionIssuancePurpose
    issuance_id: UUID
    expected_work_order_id: UUID
    expected_public_id: str


@dataclass(frozen=True, slots=True, kw_only=True)
class CompletionIssuanceEvidence:
    issuance_id: UUID
    work_order_id: UUID
    public_id: str
    project_id: UUID
    task_id: UUID | None
    plan_id: UUID
    revision_id: UUID
    approval_id: UUID
    digest: str
    infrastructure: InfrastructureReference
    purpose: WorkOrderPurpose
    technology: EngineeringTechnology
