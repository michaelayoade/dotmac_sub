"""Immutable completion observations and independent staff acceptance contracts."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from uuid import UUID

from app.services.action_readiness import ActionReadiness
from app.services.object_storage import StreamResult
from app.services.owner_commands import CommandContext
from app.services.permission_admission import PermissionAdmission
from app.services.topology.outage_completion_contracts import ComplaintResolutionScope

OWNER = "operations.field_completion"
CONCERN = "issued work-order completion review"
READ = "operations:completion-review:read"
WRITE = "operations:completion-review:write"


class ReviewState(StrEnum):
    legacy_not_reviewed = "legacy_not_reviewed"
    submitted = "submitted"
    accepted = "accepted"
    rejected = "rejected"
    revoked = "revoked"
    stale = "stale"


class CriterionVerdict(StrEnum):
    passed = "passed"
    failed = "failed"


@dataclass(frozen=True, slots=True)
class CriterionObservation:
    criterion_id: UUID
    observation: str
    attachment_ids: tuple[UUID, ...] = ()


@dataclass(frozen=True, slots=True)
class CriterionResult:
    criterion_id: UUID
    verdict: CriterionVerdict
    reason: str


@dataclass(frozen=True, slots=True, kw_only=True)
class PublishCompletionReviewPolicy:
    context: CommandContext
    access: PermissionAdmission
    scope: CompletionPolicyScope
    expected_generation: int
    independent_reviewer_required: bool


@dataclass(frozen=True, slots=True, kw_only=True)
class SubmitCompletionEvidence:
    context: CommandContext
    requester_system_user_id: UUID
    enrollment_id: UUID
    criteria_revision_id: UUID
    completed_event_id: UUID
    assignment_generation: UUID
    expected_generation: int
    policy_revision_id: UUID
    observations: tuple[CriterionObservation, ...]
    expected_work_order_public_id: str | None = None


@dataclass(frozen=True, slots=True, kw_only=True)
class DecideCompletionReview:
    context: CommandContext
    access: PermissionAdmission
    enrollment_id: UUID
    submission_id: UUID
    expected_generation: int
    policy_revision_id: UUID
    results: tuple[CriterionResult, ...]
    expected_work_order_public_id: str | None = None


@dataclass(frozen=True, slots=True, kw_only=True)
class RevokeCompletionReview:
    context: CommandContext
    access: PermissionAdmission
    enrollment_id: UUID
    decision_id: UUID
    expected_generation: int
    expected_work_order_public_id: str | None = None


@dataclass(frozen=True, slots=True, kw_only=True)
class CompletionReviewQuery:
    access: PermissionAdmission
    enrollment_id: UUID


@dataclass(frozen=True, slots=True, kw_only=True)
class CompletionReviewOutcome:
    enrollment_id: UUID | None
    policy_revision_id: UUID | None
    submission_id: UUID | None
    decision_id: UUID | None
    generation: int
    state: ReviewState
    replayed: bool = False
    criteria_revision_id: UUID | None = None
    criteria_generation: int = 0
    engineering_issuance_id: UUID | None = None
    provenance: CompletionProvenance | None = None
    completed_event_id: UUID | None = None


@dataclass(frozen=True, slots=True, kw_only=True)
class CompletionReviewRecord:
    decided_at: datetime
    id: UUID
    submission_id: UUID
    state: ReviewState
    reviewer_system_user_id: UUID
    reason: str
    policy_revision_id: UUID
    results: tuple[CriterionResult, ...]


@dataclass(frozen=True, slots=True, kw_only=True)
class CompletionSubmissionRecord:
    id: UUID
    policy_revision_id: UUID
    criteria_revision_id: UUID
    completed_event_id: UUID
    assignment_generation: UUID
    actor_system_user_id: UUID
    actor_vendor_user_id: UUID | None
    submitted_at: datetime
    observations: tuple[CriterionObservation, ...]
    criteria: tuple[IssuedCompletionCriterion, ...] = ()


@dataclass(frozen=True, slots=True, kw_only=True)
class CompletionReviewHistory:
    enrollment_id: UUID
    decisions: tuple[CompletionReviewRecord, ...]
    submissions: tuple[CompletionSubmissionRecord, ...]


class CompletionWorkspacePurpose(StrEnum):
    read = "read"
    decide = "decide"
    revoke = "revoke"
    enroll = "enroll"
    publish_criteria = "publish_criteria"


class CompletionPolicyPurpose(StrEnum):
    read = "read"
    publish_policy = "publish_policy"


@dataclass(frozen=True, slots=True, kw_only=True)
class CompletionPolicyQuery:
    access: PermissionAdmission
    scope: CompletionPolicyScope
    purpose: CompletionPolicyPurpose = CompletionPolicyPurpose.read


@dataclass(frozen=True, slots=True, kw_only=True)
class CompletionPolicySnapshot:
    scope: CompletionPolicyScope
    revision_id: UUID | None
    generation: int
    independent_reviewer_required: bool | None
    reason: str | None
    publish_readiness: ActionReadiness


@dataclass(frozen=True, slots=True, kw_only=True)
class IssuedCompletionCriterion:
    id: UUID
    order: int
    statement: str
    verification_method: str


@dataclass(frozen=True, slots=True, kw_only=True)
class CompletionWorkspaceQuery:
    access: PermissionAdmission
    work_order_public_id: str
    purpose: CompletionWorkspacePurpose = CompletionWorkspacePurpose.read


@dataclass(frozen=True, slots=True, kw_only=True)
class CompletionWorkspace:
    work_order_id: str
    engineering_issuance_id: UUID | None
    provenance: CompletionProvenance | None
    criteria_revision_id: UUID | None
    criteria_generation: int
    completed_event_id: UUID | None
    project_id: UUID | None
    task_id: UUID | None
    enrollment_id: UUID | None
    assignment_generation: UUID | None
    current: CompletionReviewOutcome
    history: CompletionReviewHistory | None
    current_submission: CompletionSubmissionRecord | None
    current_decision: CompletionReviewRecord | None
    policy: CompletionPolicySnapshot | None
    criteria: tuple[IssuedCompletionCriterion, ...]
    decide_readiness: ActionReadiness
    revoke_readiness: ActionReadiness
    enroll_readiness: ActionReadiness
    publish_criteria_readiness: ActionReadiness


@dataclass(frozen=True, slots=True, kw_only=True)
class FieldSubmissionPreviewQuery:
    requester_system_user_id: UUID
    work_order_public_id: str


@dataclass(frozen=True, slots=True, kw_only=True)
class FieldSubmissionPreview:
    work_order_id: str
    engineering_issuance_id: UUID | None
    provenance: CompletionProvenance | None
    criteria_revision_id: UUID | None
    criteria_generation: int
    completed_event_id: UUID | None
    enrollment_id: UUID | None
    assignment_generation: UUID | None
    generation: int
    policy_revision_id: UUID | None
    review_state: ReviewState
    criteria: tuple[IssuedCompletionCriterion, ...]
    submission_readiness: ActionReadiness

    @property
    def submission_available(self) -> bool:
        return self.submission_readiness.is_ready

    @property
    def submission_unavailable_reason(self) -> str | None:
        blockers = self.submission_readiness.blocking_blockers
        return blockers[0].customer_message if blockers else None


@dataclass(frozen=True, slots=True, kw_only=True)
class CompletionEvidenceQuery:
    access: PermissionAdmission
    enrollment_id: UUID
    submission_id: UUID
    attachment_id: UUID
    purpose: CompletionWorkspacePurpose = CompletionWorkspacePurpose.read


@dataclass(frozen=True, slots=True, kw_only=True)
class CompletionEvidenceDisplay:
    attachment_id: UUID
    filename: str
    content_type: str
    file_size: int
    checksum: str


@dataclass(frozen=True, slots=True, kw_only=True)
class CompletionEvidenceContent:
    evidence: CompletionEvidenceDisplay
    stream: StreamResult


class CompletionProvenance(StrEnum):
    engineering_issuance = "engineering_issuance"
    native_work_order = "native_work_order"


class CompletionPolicyScopeKind(StrEnum):
    project = "project"
    work_order = "work_order"


@dataclass(frozen=True, slots=True, kw_only=True)
class CompletionPolicyScope:
    kind: CompletionPolicyScopeKind
    project_id: UUID | None = None
    work_order_public_id: str | None = None

    def __post_init__(self) -> None:
        if self.kind == CompletionPolicyScopeKind.project:
            valid = self.project_id is not None and self.work_order_public_id is None
        elif self.kind == CompletionPolicyScopeKind.work_order:
            valid = self.project_id is None and bool(
                self.work_order_public_id and self.work_order_public_id.strip()
            )
        else:
            valid = False
        if not valid:
            raise ValueError("Exactly one explicit completion policy scope is required")


@dataclass(frozen=True, slots=True, kw_only=True)
class EnrollCompletionReview:
    context: CommandContext
    access: PermissionAdmission
    work_order_public_id: str
    provenance: CompletionProvenance
    policy_scope: CompletionPolicyScope
    engineering_issuance_id: UUID | None = None
    criteria: tuple[IssuedCompletionCriterion, ...] = ()


@dataclass(frozen=True, slots=True, kw_only=True)
class PublishCompletionCriteria:
    context: CommandContext
    access: PermissionAdmission
    enrollment_id: UUID
    expected_generation: int
    criteria: tuple[IssuedCompletionCriterion, ...]
    expected_work_order_public_id: str | None = None


@dataclass(frozen=True, slots=True)
class TicketCompletionBasisQuery:
    ticket_id: UUID


@dataclass(frozen=True, slots=True)
class TicketCompletionBasisEntry:
    work_order_id: UUID
    enrollment_id: UUID
    criteria_revision_id: UUID
    policy_revision_id: UUID
    assignment_generation: UUID
    completed_event_id: UUID
    submission_id: UUID
    accepted_decision_id: UUID
    review_generation: int
    effective_work_order_id: UUID | None = None
    effective_enrollment_id: UUID | None = None
    replacement_decision_ids: tuple[UUID, ...] = ()


@dataclass(frozen=True, slots=True)
class TicketCompletionBasis:
    ticket_id: UUID
    entries: tuple[TicketCompletionBasisEntry, ...]
    shared_incidents: tuple[ComplaintResolutionScope, ...] = ()


@dataclass(frozen=True, slots=True)
class LinkedTicketEnrollmentQuery:
    ticket_id: UUID


@dataclass(frozen=True, slots=True)
class LinkedTicketEnrollments:
    ticket_id: UUID
    work_order_ids: tuple[UUID, ...]


@dataclass(frozen=True, slots=True, kw_only=True)
class SupportTicketCompletionPreviewQuery:
    ticket_id: UUID
    access: PermissionAdmission


@dataclass(frozen=True, slots=True)
class UnableToCompleteFailure:
    event_id: UUID
    assignment_generation: UUID


@dataclass(frozen=True, slots=True)
class RejectedSubmissionFailure:
    submission_id: UUID
    decision_id: UUID
    review_generation: int


FailureBasis = UnableToCompleteFailure | RejectedSubmissionFailure


@dataclass(frozen=True, slots=True)
class CriterionCoverage:
    source_criterion_id: UUID
    replacement_criterion_id: UUID


class ReplacementState(StrEnum):
    approved = "approved"
    revoked = "revoked"


@dataclass(frozen=True, slots=True, kw_only=True)
class ReviewFailedScopeReplacement:
    context: CommandContext
    access: PermissionAdmission
    source_enrollment_id: UUID
    replacement_enrollment_id: UUID
    expected_generation: int
    failure_basis: FailureBasis
    source_criteria_revision_id: UUID
    replacement_criteria_revision_id: UUID
    source_policy_revision_id: UUID
    replacement_policy_revision_id: UUID
    source_assignment_generation: UUID
    replacement_assignment_generation: UUID
    coverage: tuple[CriterionCoverage, ...]
    expected_work_order_public_id: str | None = None


@dataclass(frozen=True, slots=True, kw_only=True)
class RevokeFailedScopeReplacement:
    context: CommandContext
    access: PermissionAdmission
    source_enrollment_id: UUID
    decision_id: UUID
    expected_generation: int
    expected_work_order_public_id: str | None = None


@dataclass(frozen=True, slots=True, kw_only=True)
class FailedScopeReplacementOutcome:
    source_enrollment_id: UUID
    replacement_enrollment_id: UUID
    decision_id: UUID
    generation: int
    state: ReplacementState
    replayed: bool = False


class ReplacementWorkspacePurpose(StrEnum):
    read = "read"
    review = "review"
    revoke = "revoke"


@dataclass(frozen=True, slots=True, kw_only=True)
class FailedScopeReplacementWorkspaceQuery:
    access: PermissionAdmission
    work_order_public_id: str
    purpose: ReplacementWorkspacePurpose = ReplacementWorkspacePurpose.read


@dataclass(frozen=True, slots=True, kw_only=True)
class ReplacementEnrollmentSnapshot:
    enrollment_id: UUID
    work_order_public_id: str
    title: str
    status: str
    criteria_revision_id: UUID
    policy_revision_id: UUID | None
    assignment_generation: UUID | None
    criteria: tuple[IssuedCompletionCriterion, ...]
    review_generation: int
    failure_basis: FailureBasis | None
    independent_reviewer_required: bool | None = None
    current_review_state: ReviewState = ReviewState.legacy_not_reviewed


@dataclass(frozen=True, slots=True, kw_only=True)
class FailedScopeReplacementRecord:
    decision_id: UUID
    source_enrollment_id: UUID
    replacement_enrollment_id: UUID
    replacement_work_order_public_id: str
    replacement_title: str
    generation: int
    state: ReplacementState
    reason: str
    decided_at: datetime
    coverage: tuple[CriterionCoverage, ...]


@dataclass(frozen=True, slots=True, kw_only=True)
class FailedScopeReplacementWorkspace:
    source: ReplacementEnrollmentSnapshot
    expected_generation: int
    current_decision: FailedScopeReplacementRecord | None
    history: tuple[FailedScopeReplacementRecord, ...]
    candidates: tuple[ReplacementEnrollmentSnapshot, ...]
    review_readiness: ActionReadiness
    revoke_readiness: ActionReadiness


@dataclass(frozen=True, slots=True)
class NativeTicketReplacementScope:
    ticket_id: UUID
    subscriber_id: UUID


@dataclass(frozen=True, slots=True)
class EngineeringReplacementScope:
    project_id: UUID
    task_id: UUID | None


@dataclass(frozen=True, slots=True)
class OutageInfrastructureReplacementScope:
    infrastructure_ticket_id: UUID


ReplacementScope = (
    NativeTicketReplacementScope
    | EngineeringReplacementScope
    | OutageInfrastructureReplacementScope
)


@dataclass(frozen=True, slots=True, kw_only=True)
class CurrentReplacementEvidenceQuery:
    access: PermissionAdmission
    source_work_order_id: UUID
    decision_id: UUID
    failure_basis: FailureBasis


@dataclass(frozen=True, slots=True, kw_only=True)
class CurrentReplacementEvidence:
    decision_id: UUID
    source_work_order_id: UUID
    replacement_work_order_id: UUID
    source_enrollment_id: UUID
    replacement_enrollment_id: UUID
    generation: int


class CompletionBasisParticipant(StrEnum):
    support_confirmation = "support_confirmation"
    project_completion = "project_completion"
    project_task_completion = "project_task_completion"


@dataclass(frozen=True, slots=True, kw_only=True)
class ScopedCompletionBasisQuery:
    required_work_order_ids: tuple[UUID, ...]
    participant: CompletionBasisParticipant
    expected_enrollment_ids: tuple[UUID, ...] | None = None


@dataclass(frozen=True, slots=True, kw_only=True)
class ScopedCompletionBasisPreviewQuery:
    required_work_order_ids: tuple[UUID, ...]
    participant: CompletionBasisParticipant
    access: PermissionAdmission
    expected_enrollment_ids: tuple[UUID, ...] | None = None


@dataclass(frozen=True, slots=True)
class ScopedCompletionBasis:
    entries: tuple[TicketCompletionBasisEntry, ...]
