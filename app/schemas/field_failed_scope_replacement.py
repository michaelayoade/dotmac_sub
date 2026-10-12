"""Immutable replacement review transport inputs; staff identity is server-owned."""

from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, model_validator

from app.schemas.field_completion_review import CompletionIntent
from app.services.field.completion_review_contracts import (
    WRITE,
    CriterionCoverage,
    RejectedSubmissionFailure,
    ReviewFailedScopeReplacement,
    RevokeFailedScopeReplacement,
    UnableToCompleteFailure,
)
from app.services.permission_admission import PermissionAdmission


class FailureInput(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    kind: Literal["unable_to_complete", "rejected_submission"]
    event_id: UUID | None = None
    assignment_generation: UUID | None = None
    submission_id: UUID | None = None
    decision_id: UUID | None = None
    review_generation: int | None = Field(default=None, ge=0)

    @model_validator(mode="after")
    def validate_failure(self) -> "FailureInput":
        self.value()
        return self

    def value(self) -> UnableToCompleteFailure | RejectedSubmissionFailure:
        if self.kind == "unable_to_complete":
            if any(
                value is not None
                for value in (
                    self.submission_id,
                    self.decision_id,
                    self.review_generation,
                )
            ):
                raise ValueError("Failure basis must name exactly one failure kind")
            if self.event_id is None or self.assignment_generation is None:
                raise ValueError("Exact failure event and assignment are required")
            return UnableToCompleteFailure(self.event_id, self.assignment_generation)
        if self.event_id is not None or self.assignment_generation is not None:
            raise ValueError("Failure basis must name exactly one failure kind")
        if (
            self.submission_id is None
            or self.decision_id is None
            or self.review_generation is None
        ):
            raise ValueError("Exact rejected submission and decision are required")
        return RejectedSubmissionFailure(
            self.submission_id, self.decision_id, self.review_generation
        )


class CoverageInput(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    source_criterion_id: UUID
    replacement_criterion_id: UUID


class ReplacementReviewInput(CompletionIntent):
    replacement_enrollment_id: UUID
    expected_generation: int = Field(ge=0)
    failure_basis: FailureInput
    source_criteria_revision_id: UUID
    replacement_criteria_revision_id: UUID
    source_policy_revision_id: UUID
    replacement_policy_revision_id: UUID
    source_assignment_generation: UUID
    replacement_assignment_generation: UUID
    coverage: tuple[CoverageInput, ...] = Field(min_length=1, max_length=100)

    def command(
        self,
        *,
        access: PermissionAdmission,
        source_enrollment_id: UUID,
        work_order_public_id: str | None = None,
    ) -> ReviewFailedScopeReplacement:
        return ReviewFailedScopeReplacement(
            context=self._context(actor=access.principal_id, scope=WRITE),
            access=access,
            source_enrollment_id=source_enrollment_id,
            expected_work_order_public_id=work_order_public_id,
            replacement_enrollment_id=self.replacement_enrollment_id,
            expected_generation=self.expected_generation,
            failure_basis=self.failure_basis.value(),
            source_criteria_revision_id=self.source_criteria_revision_id,
            replacement_criteria_revision_id=self.replacement_criteria_revision_id,
            source_policy_revision_id=self.source_policy_revision_id,
            replacement_policy_revision_id=self.replacement_policy_revision_id,
            source_assignment_generation=self.source_assignment_generation,
            replacement_assignment_generation=self.replacement_assignment_generation,
            coverage=tuple(
                CriterionCoverage(x.source_criterion_id, x.replacement_criterion_id)
                for x in self.coverage
            ),
        )


class ReplacementRevocationInput(CompletionIntent):
    decision_id: UUID
    expected_generation: int = Field(ge=0)

    def command(
        self,
        *,
        access: PermissionAdmission,
        source_enrollment_id: UUID,
        work_order_public_id: str | None = None,
    ) -> RevokeFailedScopeReplacement:
        return RevokeFailedScopeReplacement(
            context=self._context(actor=access.principal_id, scope=WRITE),
            access=access,
            source_enrollment_id=source_enrollment_id,
            expected_work_order_public_id=work_order_public_id,
            decision_id=self.decision_id,
            expected_generation=self.expected_generation,
        )
