"""Typed native creation participant for the fiber verification coordinator."""

from dataclasses import dataclass
from datetime import datetime
from typing import Literal
from uuid import UUID

from pydantic import AliasChoices, ConfigDict, Field, JsonValue

from app.schemas.dispatch import WorkOrderHeaderRead
from app.services.network.fiber_field_verification_job_plan_contracts import (
    FiberVerificationPlanProvenance,
)
from app.services.permission_admission import PermissionAdmission
from app.services.project_engineering_plan_contracts import (
    CurrentEngineeringPlanApproval,
)
from app.services.work_order_creation_values import WorkOrderCreationPriority
from app.services.work_order_templates import ExplicitWorkOrderClassification


@dataclass(frozen=True, slots=True, kw_only=True)
class NativeWorkOrderCreateCommand:
    public_id: str
    subscriber_id: UUID
    title: str
    description: str | None
    priority: str
    address: str | None
    scheduled_start: datetime | None
    scheduled_end: datetime | None
    idempotency_key: str
    provenance: FiberVerificationPlanProvenance
    access: PermissionAdmission
    request_id: str | None = None
    kind: Literal["fiber_verification"] = "fiber_verification"


@dataclass(frozen=True, slots=True)
class NativeWorkOrderCreated:
    id: UUID
    public_id: str
    revision: datetime
    scheduled_start: datetime | None
    scheduled_end: datetime | None
    replayed: bool
    status: str
    title: str
    subscriber_id: UUID
    priority: str | None


class NativeWorkOrderSnapshot(WorkOrderHeaderRead):
    """Immutable canonical full header projection for typed coordinators."""

    model_config = ConfigDict(from_attributes=True, populate_by_name=True, frozen=True)

    metadata_: dict[str, JsonValue] | None = Field(
        default=None,
        validation_alias=AliasChoices("metadata_", "metadata"),
        serialization_alias="metadata",
    )


@dataclass(frozen=True, slots=True, kw_only=True)
class ProjectInfrastructureProvenance:
    approval: CurrentEngineeringPlanApproval
    classification: ExplicitWorkOrderClassification
    issuer_person_id: UUID
    kind: Literal["project_infrastructure"] = "project_infrastructure"


@dataclass(frozen=True, slots=True, kw_only=True)
class ProjectNativeWorkOrderCreateCommand:
    public_id: str
    title: str
    description: str | None
    priority: WorkOrderCreationPriority
    provenance: ProjectInfrastructureProvenance
    access: PermissionAdmission
    kind: Literal["project_infrastructure"] = "project_infrastructure"


@dataclass(frozen=True, slots=True)
class ProjectNativeWorkOrderCreated:
    id: UUID
    public_id: str
    revision: datetime
    scheduled_start: datetime | None
    scheduled_end: datetime | None
    replayed: bool
    status: Literal["draft"]
    title: str
    subscriber_id: None
    priority: WorkOrderCreationPriority
