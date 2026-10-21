"""Shared immutable completion-review transport inputs; identity stays server-owned."""

from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

from app.services.field.completion_review_contracts import (
    WRITE,
    CompletionPolicyScope,
    CompletionProvenance,
    CriterionObservation,
    CriterionResult,
    CriterionVerdict,
    DecideCompletionReview,
    EnrollCompletionReview,
    IssuedCompletionCriterion,
    PublishCompletionCriteria,
    PublishCompletionReviewPolicy,
    ReviewState,
    RevokeCompletionReview,
    SubmitCompletionEvidence,
)
from app.services.owner_commands import CommandContext
from app.services.permission_admission import PermissionAdmission


class CompletionIntent(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    confirmed: Literal[True]
    intent_id: UUID
    reason: str = Field(min_length=1, max_length=1000)

    def _context(self, *, actor: UUID, scope: str) -> CommandContext:
        return CommandContext(
            command_id=self.intent_id,
            correlation_id=self.intent_id,
            actor=str(actor),
            scope=scope,
            reason=self.reason,
            idempotency_key=str(self.intent_id),
        )


class CompletionPolicyInput(CompletionIntent):
    expected_generation: int = Field(ge=0)
    independent_reviewer_required: bool

    def command(
        self, *, access: PermissionAdmission, scope: CompletionPolicyScope
    ) -> PublishCompletionReviewPolicy:
        return PublishCompletionReviewPolicy(
            context=self._context(actor=access.principal_id, scope=WRITE),
            access=access,
            scope=scope,
            expected_generation=self.expected_generation,
            independent_reviewer_required=self.independent_reviewer_required,
        )


class CompletionObservationInput(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    criterion_id: UUID
    observation: str = Field(min_length=1, max_length=8000)
    attachment_ids: tuple[UUID, ...] = ()

    def value(self) -> CriterionObservation:
        return CriterionObservation(
            self.criterion_id, self.observation, self.attachment_ids
        )


class CompletionSubmissionInput(CompletionIntent):
    enrollment_id: UUID
    criteria_revision_id: UUID
    completed_event_id: UUID
    assignment_generation: UUID
    expected_generation: int = Field(ge=0)
    policy_revision_id: UUID
    observations: tuple[CompletionObservationInput, ...] = Field(min_length=1)

    def command(
        self, *, requester_system_user_id: UUID, work_order_public_id: str
    ) -> SubmitCompletionEvidence:
        return SubmitCompletionEvidence(
            context=self._context(
                actor=requester_system_user_id, scope="field:completion:submit"
            ),
            requester_system_user_id=requester_system_user_id,
            enrollment_id=self.enrollment_id,
            criteria_revision_id=self.criteria_revision_id,
            completed_event_id=self.completed_event_id,
            assignment_generation=self.assignment_generation,
            expected_generation=self.expected_generation,
            policy_revision_id=self.policy_revision_id,
            observations=tuple(item.value() for item in self.observations),
            expected_work_order_public_id=work_order_public_id,
        )


class CompletionCriterionResultInput(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    criterion_id: UUID
    verdict: CriterionVerdict
    reason: str = Field(min_length=1, max_length=4000)

    def value(self) -> CriterionResult:
        return CriterionResult(self.criterion_id, self.verdict, self.reason)


class CompletionDecisionInput(CompletionIntent):
    submission_id: UUID
    expected_generation: int = Field(ge=0)
    policy_revision_id: UUID
    results: tuple[CompletionCriterionResultInput, ...] = Field(min_length=1)

    def command(
        self,
        *,
        access: PermissionAdmission,
        enrollment_id: UUID,
        work_order_public_id: str | None = None,
    ) -> DecideCompletionReview:
        return DecideCompletionReview(
            context=self._context(actor=access.principal_id, scope=WRITE),
            access=access,
            enrollment_id=enrollment_id,
            submission_id=self.submission_id,
            expected_generation=self.expected_generation,
            policy_revision_id=self.policy_revision_id,
            results=tuple(item.value() for item in self.results),
            expected_work_order_public_id=work_order_public_id,
        )


class CompletionRevocationInput(CompletionIntent):
    decision_id: UUID
    expected_generation: int = Field(ge=0)

    def command(
        self,
        *,
        access: PermissionAdmission,
        enrollment_id: UUID,
        work_order_public_id: str | None = None,
    ) -> RevokeCompletionReview:
        return RevokeCompletionReview(
            context=self._context(actor=access.principal_id, scope=WRITE),
            access=access,
            enrollment_id=enrollment_id,
            decision_id=self.decision_id,
            expected_work_order_public_id=work_order_public_id,
            expected_generation=self.expected_generation,
        )


class CompletionCriterionFormValues(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    criterion_id: str
    verdict: str = ""
    reason: str = ""


class CompletionCriterionAuthoringValues(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    criterion_id: str
    statement: str = ""
    verification_method: str = ""


class CompletionFormValues(BaseModel):
    """Original browser strings and intent retained on every refusal."""

    model_config = ConfigDict(frozen=True, extra="forbid")
    intent_id: str = ""
    enrollment_id: str = ""
    criteria_generation: str = ""
    provenance: str = ""
    engineering_issuance_id: str = ""
    policy_scope_kind: str = ""
    policy_project_id: str = ""
    policy_work_order_public_id: str = ""
    expected_generation: str = ""
    policy_revision_id: str = ""
    submission_id: str = ""
    decision_id: str = ""
    independent_reviewer_required: str = "select"
    reason: str = ""
    results: tuple[CompletionCriterionFormValues, ...] = ()
    criteria: tuple[CompletionCriterionAuthoringValues, ...] = ()


class FieldCompletionPreviewResponse(BaseModel):
    """Owner readiness is visible for enrolled and unenrolled native jobs."""

    model_config = ConfigDict(frozen=True, extra="forbid", from_attributes=True)
    work_order_id: str
    enrollment_id: UUID | None
    criteria_revision_id: UUID | None
    completed_event_id: UUID | None
    provenance: CompletionProvenance | None
    engineering_issuance_id: UUID | None
    assignment_generation: UUID | None
    generation: int
    criteria_generation: int
    policy_revision_id: UUID | None
    review_state: ReviewState
    criteria: tuple[IssuedCompletionCriterion, ...]
    submission_available: bool
    submission_unavailable_reason: str | None


class CompletionCriterionInput(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    id: UUID
    order: int = Field(ge=0)
    statement: str = Field(min_length=1)
    verification_method: str = Field(min_length=1)

    def value(self) -> IssuedCompletionCriterion:
        return IssuedCompletionCriterion(
            id=self.id,
            order=self.order,
            statement=self.statement,
            verification_method=self.verification_method,
        )


class CompletionEnrollmentInput(CompletionIntent):
    provenance: CompletionProvenance
    policy_scope: CompletionPolicyScope
    engineering_issuance_id: UUID | None = None
    criteria: tuple[CompletionCriterionInput, ...] = ()

    def command(
        self, *, access: PermissionAdmission, work_order_public_id: str
    ) -> EnrollCompletionReview:
        return EnrollCompletionReview(
            context=self._context(actor=access.principal_id, scope=WRITE),
            access=access,
            work_order_public_id=work_order_public_id,
            provenance=self.provenance,
            policy_scope=self.policy_scope,
            engineering_issuance_id=self.engineering_issuance_id,
            criteria=tuple(item.value() for item in self.criteria),
        )


class CompletionCriteriaInput(CompletionIntent):
    expected_generation: int = Field(ge=0)
    criteria: tuple[CompletionCriterionInput, ...] = Field(min_length=1)

    def command(
        self,
        *,
        access: PermissionAdmission,
        enrollment_id: UUID,
        work_order_public_id: str | None = None,
    ) -> PublishCompletionCriteria:
        return PublishCompletionCriteria(
            context=self._context(actor=access.principal_id, scope=WRITE),
            access=access,
            enrollment_id=enrollment_id,
            expected_generation=self.expected_generation,
            criteria=tuple(item.value() for item in self.criteria),
            expected_work_order_public_id=work_order_public_id,
        )
