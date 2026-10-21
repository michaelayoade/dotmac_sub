"""Exact issued shared-outage scope; no restoration or customer closure decisions."""

from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from uuid import UUID

from app.services.permission_admission import PermissionAdmission
from app.services.work_order_templates import (
    WorkOrderAccessTechnology,
    WorkOrderPurpose,
)


class OutageIssuedTarget(StrEnum):
    basestation = "basestation"
    fdh_cabinet = "fdh-cabinet"
    node = "node"
    unresolved = "unresolved"


class OutageCompletionPurpose(StrEnum):
    read = "read"
    review = "review"


class EmptySharedScopeReview(StrEnum):
    restored_without_issued_field_scope = "restored_without_issued_field_scope"


@dataclass(frozen=True, slots=True)
class ComplaintGraphLocksQuery:
    ticket_ids: tuple[UUID, ...]
    incident_ids: tuple[UUID, ...] = ()


@dataclass(frozen=True, slots=True)
class ComplaintGraphLocks:
    incident_ids: tuple[UUID, ...]
    ticket_ids: tuple[UUID, ...]


@dataclass(frozen=True, slots=True)
class OutageCompletionIssuanceQuery:
    work_order_id: UUID
    access: PermissionAdmission
    purpose: OutageCompletionPurpose


@dataclass(frozen=True, slots=True)
class OutageCompletionIssuance:
    issuance_link_id: UUID
    work_order_id: UUID
    work_order_public_id: str
    work_order_title: str
    incident_id: UUID
    infrastructure_ticket_id: UUID
    issued_revision_id: UUID
    issued_revision_sequence: int
    membership_token: str
    target_type: OutageIssuedTarget
    target_id: UUID | None
    project_id: UUID | None
    task_id: UUID | None
    purpose: WorkOrderPurpose | None
    access_technology: WorkOrderAccessTechnology | None


@dataclass(frozen=True, slots=True)
class ComplaintResolutionScopesQuery:
    ticket_id: UUID


@dataclass(frozen=True, slots=True)
class ComplaintResolutionScopesPreviewQuery:
    ticket_id: UUID
    access: PermissionAdmission


@dataclass(frozen=True, slots=True)
class ComplaintResolutionScope:
    complaint_link_id: UUID
    incident_id: UUID
    incident_label: str
    subscription_id: UUID
    original_receipt_id: UUID
    original_receipt_ref: str
    original_revision_id: UUID
    original_revision_sequence: int
    infrastructure_ticket_id: UUID
    reviewed_revision_id: UUID
    reviewed_revision_sequence: int
    membership_token: str
    resolved_at: datetime
    issuances: tuple[OutageCompletionIssuance, ...]


@dataclass(frozen=True, slots=True)
class ComplaintResolutionScopes:
    ticket_id: UUID
    incidents: tuple[ComplaintResolutionScope, ...]


@dataclass(frozen=True, slots=True)
class ReviewedComplaintResolutionScope:
    complaint_link_id: UUID
    reviewed_revision_id: UUID
    membership_token: str
    resolved_at: datetime
    issuance_link_ids: tuple[UUID, ...]
    empty_scope_review: EmptySharedScopeReview | None = None


@dataclass(frozen=True, slots=True)
class OutageReplacementCoverageQuery:
    source_work_order_id: UUID
    replacement_work_order_id: UUID


@dataclass(frozen=True, slots=True)
class OutageReplacementCoverage:
    source: OutageCompletionIssuance
    replacement: OutageCompletionIssuance


@dataclass(frozen=True, slots=True)
class OutageReplacementCoveragePreviewQuery(OutageReplacementCoverageQuery):
    access: PermissionAdmission
    purpose: OutageCompletionPurpose


@dataclass(frozen=True, slots=True)
class OutageCompletionGraphQuery:
    work_order_id: UUID


class OutageCoverageParticipant(StrEnum):
    support_confirmation = "support_confirmation"
    project_completion = "project_completion"
    project_task_completion = "project_task_completion"
    completion_review = "completion_review"


@dataclass(frozen=True, slots=True)
class ScopedOutageCoveragePreviewQuery(OutageReplacementCoverageQuery):
    access: PermissionAdmission
    participant: OutageCoverageParticipant


@dataclass(frozen=True, slots=True)
class CompletionScopeGraphLocksQuery:
    required_work_order_ids: tuple[UUID, ...]
