"""Typed explicit classification command, independent of legacy creation."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from uuid import UUID

from app.services.field.work_order_kind import WorkOrderKind
from app.services.field.work_order_status import WorkOrderStatus
from app.services.owner_commands import CommandContext
from app.services.work_order_templates import (
    ClassificationSource,
    ExplicitWorkOrderClassification,
    WorkOrderAccessTechnology,
    WorkOrderPurpose,
    WorkOrderTemplateRecommendation,
)


@dataclass(frozen=True, kw_only=True)
class ClassifyWorkOrderCommand:
    context: CommandContext
    work_order_id: UUID
    expected_revision: datetime
    classification: ExplicitWorkOrderClassification


@dataclass(frozen=True, kw_only=True)
class WorkOrderClassificationOutcome:
    work_order_id: UUID
    public_id: str
    revision: datetime
    recommendation: WorkOrderTemplateRecommendation
    replayed: bool


@dataclass(frozen=True, kw_only=True)
class WorkOrderClassifiedEventPayload:
    work_order_id: UUID
    work_order_public_id: str
    purpose: WorkOrderPurpose
    access_technology: WorkOrderAccessTechnology
    classification_source: ClassificationSource
    template_version: int
    revision: datetime
    command_id: UUID
    schema_version: int = 1


class ClassificationEligibility(StrEnum):
    ALLOWED = "allowed"
    INACTIVE = "inactive"
    EXECUTION_STARTED = "execution_started"
    STATUS_BLOCKED = "status_blocked"


@dataclass(frozen=True, kw_only=True)
class WorkOrderClassificationQuery:
    work_order_id: UUID


@dataclass(frozen=True, kw_only=True)
class WorkOrderClassificationSnapshot:
    work_order_id: UUID
    public_id: str
    title: str
    kind: WorkOrderKind
    status: WorkOrderStatus
    revision: datetime
    eligibility: ClassificationEligibility
    blocked_reason: str | None
    classification: ExplicitWorkOrderClassification | None
    recommendation: WorkOrderTemplateRecommendation | None
    purpose_options: tuple[WorkOrderPurpose, ...]
    technology_options: tuple[WorkOrderAccessTechnology, ...]
