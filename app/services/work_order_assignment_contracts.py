"""Immutable contracts for the native work-order assignment owner."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from uuid import UUID


@dataclass(frozen=True)
class TechnicianAssignmentTarget:
    technician_id: UUID


@dataclass(frozen=True)
class VendorAssignmentTarget:
    vendor_id: UUID


AssignmentTarget = TechnicianAssignmentTarget | VendorAssignmentTarget


@dataclass(frozen=True, kw_only=True)
class WorkOrderAssignmentQuery:
    work_order_public_id: str
    target: AssignmentTarget
    scheduled_start: datetime | None = None
    scheduled_end: datetime | None = None
    status: str = "dispatched"


@dataclass(frozen=True, kw_only=True)
class WorkOrderAssignmentCommand(WorkOrderAssignmentQuery):
    reason: str | None = None
    dispatch_rule_id: UUID | None = None
    expected_revision: datetime | None = None


@dataclass(frozen=True)
class WorkOrderAssignmentState:
    status: str
    technician_id: UUID | None
    vendor_id: UUID | None
    person_id: UUID | None
    technician_name: str | None
    vendor_name: str | None
    scheduled_start: datetime | None
    scheduled_end: datetime | None


@dataclass(frozen=True)
class WorkOrderAssignmentPreview:
    work_order_id: str
    revision: datetime
    previous: WorkOrderAssignmentState
    result: WorkOrderAssignmentState


@dataclass(frozen=True)
class WorkOrderAssignmentOutcome:
    queue_id: UUID
    work_order_id: str
    target: AssignmentTarget
    status: str
    revision: datetime
    replayed: bool = False


@dataclass(frozen=True)
class WorkOrderAssignmentEligibilityQuery:
    work_order_public_id: str


@dataclass(frozen=True)
class WorkOrderAssignmentEligibility:
    allowed: bool
    reason: str | None = None
