"""Immutable contracts for the native work-order assignment owner."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from uuid import UUID

from app.services.permission_admission import PermissionAdmission


@dataclass(frozen=True)
class TechnicianAssignmentTarget:
    technician_id: UUID


@dataclass(frozen=True)
class VendorAssignmentTarget:
    vendor_id: UUID
    vendor_user_id: UUID | None = None


AssignmentTarget = TechnicianAssignmentTarget | VendorAssignmentTarget


@dataclass(frozen=True, kw_only=True)
class WorkOrderAssignmentQuery:
    work_order_public_id: str
    target: AssignmentTarget
    scheduled_start: datetime | None = None
    scheduled_end: datetime | None = None
    status: str = "dispatched"
    expected_revision: datetime | None = None
    expected_assignment_generation: UUID | None = None


class AssignmentOperation(StrEnum):
    assign = "assign"
    reschedule = "reschedule"


@dataclass(frozen=True, kw_only=True)
class WorkOrderAssignmentCommand(WorkOrderAssignmentQuery):
    access: PermissionAdmission
    operation: AssignmentOperation = AssignmentOperation.assign
    reason: str | None = None
    dispatch_rule_id: UUID | None = None


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
    vendor_user_id: UUID | None = None
    vendor_user_name: str | None = None


@dataclass(frozen=True)
class WorkOrderAssignmentPreview:
    work_order_id: str
    revision: datetime
    previous: WorkOrderAssignmentState
    result: WorkOrderAssignmentState
    assignment_generation: UUID | None = None


@dataclass(frozen=True)
class WorkOrderAssignmentOutcome:
    queue_id: UUID
    work_order_id: str
    target: AssignmentTarget
    status: str
    revision: datetime
    replayed: bool = False
    assignment_generation: UUID | None = None


@dataclass(frozen=True)
class WorkOrderAssignmentEligibilityQuery:
    work_order_public_id: str


@dataclass(frozen=True)
class WorkOrderAssignmentEligibility:
    allowed: bool
    reason: str | None = None


@dataclass(frozen=True, slots=True)
class CurrentWorkOrderAssignmentQuery:
    work_order_id: UUID


@dataclass(frozen=True, slots=True)
class CurrentWorkOrderAssignment:
    queue_id: UUID
    technician_id: UUID | None
    vendor_id: UUID | None
    generation: UUID | None
    effective_at: datetime | None
    vendor_user_id: UUID | None = None


@dataclass(frozen=True, slots=True)
class VendorAssignmentCandidatesQuery:
    work_order_public_id: str
    vendor_id: UUID


@dataclass(frozen=True, slots=True)
class VendorAssignmentCandidate:
    vendor_user_id: UUID
    system_user_id: UUID
    display_name: str


@dataclass(frozen=True, slots=True)
class VendorAssignmentCandidates:
    work_order_id: str
    revision: datetime
    assignment_generation: UUID | None
    candidates: tuple[VendorAssignmentCandidate, ...]
    vendor_name: str


@dataclass(frozen=True, slots=True, kw_only=True)
class WorkOrderAssignmentPreparationQuery:
    work_order_public_id: str
    target: AssignmentTarget
    access: PermissionAdmission


@dataclass(frozen=True, slots=True)
class WorkOrderAssignmentPreparation:
    work_order_public_id: str
    target: AssignmentTarget
    access: PermissionAdmission
    current: CurrentWorkOrderAssignment | None
    principal_ids: tuple[UUID, ...]


@dataclass(frozen=True, slots=True)
class WorkOrderAssignmentReplaySnapshot:
    expected_revision: datetime | None
    expected_assignment_generation: UUID | None


@dataclass(frozen=True, slots=True, kw_only=True)
class WorkOrderLinkageFence:
    ticket_id: UUID | None
    work_order_id: UUID | None = None

    def __post_init__(self) -> None:
        if self.ticket_id is None and self.work_order_id is None:
            raise ValueError(
                "A native Ticket or WorkOrder linkage identity is required"
            )


@dataclass(frozen=True, slots=True, kw_only=True)
class CompletionEnrollmentLinkageQuery:
    work_order_id: UUID
    origin_ticket_id: UUID | None


@dataclass(frozen=True, slots=True, kw_only=True)
class CompletionEnrollmentLinkage:
    work_order_id: UUID
    origin_ticket_id: UUID | None
    subscriber_id: UUID | None
    project_id: UUID | None
    project_task_id: UUID | None


@dataclass(frozen=True, slots=True, kw_only=True)
class CompletionEnrollmentLinkagePreviewQuery(CompletionEnrollmentLinkageQuery):
    access: PermissionAdmission
