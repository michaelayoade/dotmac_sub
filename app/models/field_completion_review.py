"""Immutable issued-work completion evidence; mutable heads carry optimistic generations."""

from datetime import UTC, datetime
from uuid import UUID, uuid4

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    DateTime,
    FetchedValue,
    ForeignKey,
    ForeignKeyConstraint,
    Integer,
    String,
    Text,
    UniqueConstraint,
    event,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.db import Base
from app.models.transaction_provenance import OuterTransactionId


class CompletionEnrollment(Base):
    __tablename__ = "completion_enrollments"
    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    work_order_id: Mapped[UUID] = mapped_column(
        ForeignKey("work_order.id", ondelete="RESTRICT"), unique=True, nullable=False
    )
    public_id: Mapped[str] = mapped_column(String(64), nullable=False)
    provenance: Mapped[str] = mapped_column(String(24), nullable=False)
    engineering_issuance_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("project_work_order_issuances.id", ondelete="RESTRICT"), unique=True
    )
    project_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("projects.id", ondelete="RESTRICT")
    )
    task_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("project_tasks.id", ondelete="RESTRICT")
    )
    subscriber_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("subscribers.id", ondelete="RESTRICT")
    )
    origin_ticket_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("support_tickets.id", ondelete="RESTRICT")
    )
    policy_scope_type: Mapped[str] = mapped_column(String(12), nullable=False)
    policy_scope_id: Mapped[UUID] = mapped_column(nullable=False)
    actor_user_id: Mapped[UUID] = mapped_column(
        ForeignKey("system_users.id", ondelete="RESTRICT"), nullable=False
    )
    actor_person_id: Mapped[UUID] = mapped_column(
        ForeignKey("parties.id", ondelete="RESTRICT"), nullable=False
    )
    reason: Mapped[str] = mapped_column(Text, nullable=False)
    enrolled_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(UTC), nullable=False
    )
    __table_args__ = (
        UniqueConstraint("work_order_id", "id"),
        ForeignKeyConstraint(
            ["policy_scope_type", "policy_scope_id"],
            [
                "completion_review_policy_heads.scope_type",
                "completion_review_policy_heads.scope_id",
            ],
            ondelete="RESTRICT",
        ),
        CheckConstraint(
            "(provenance='engineering_issuance' AND engineering_issuance_id IS NOT NULL AND project_id IS NOT NULL) OR (provenance='native_work_order' AND engineering_issuance_id IS NULL)"
        ),
        CheckConstraint(
            "(policy_scope_type='project' AND policy_scope_id=project_id AND project_id IS NOT NULL) OR (policy_scope_type='work_order' AND policy_scope_id=work_order_id)"
        ),
        CheckConstraint("length(trim(reason)) > 0"),
    )


class CompletionCriteriaRevision(Base):
    __tablename__ = "completion_criteria_revisions"
    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    enrollment_id: Mapped[UUID] = mapped_column(
        ForeignKey("completion_enrollments.id", ondelete="RESTRICT"), nullable=False
    )
    revision: Mapped[int] = mapped_column(Integer, nullable=False)
    engineering_revision_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("engineering_plan_revisions.id", ondelete="RESTRICT")
    )
    actor_user_id: Mapped[UUID] = mapped_column(
        ForeignKey("system_users.id", ondelete="RESTRICT"), nullable=False
    )
    actor_person_id: Mapped[UUID] = mapped_column(
        ForeignKey("parties.id", ondelete="RESTRICT"), nullable=False
    )
    reason: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(UTC), nullable=False
    )
    creation_outer_xid: Mapped[str | None] = mapped_column(
        OuterTransactionId(), nullable=True, server_default=FetchedValue()
    )
    __table_args__ = (
        UniqueConstraint("enrollment_id", "id"),
        UniqueConstraint("enrollment_id", "revision"),
        CheckConstraint("revision>0 AND length(trim(reason))>0"),
    )


class CompletionCriterion(Base):
    __tablename__ = "completion_criteria"
    revision_id: Mapped[UUID] = mapped_column(
        ForeignKey("completion_criteria_revisions.id", ondelete="RESTRICT"),
        primary_key=True,
    )
    id: Mapped[UUID] = mapped_column(primary_key=True)
    order: Mapped[int] = mapped_column(Integer, nullable=False)
    statement: Mapped[str] = mapped_column(Text, nullable=False)
    verification_method: Mapped[str] = mapped_column(Text, nullable=False)
    __table_args__ = (
        UniqueConstraint("revision_id", "order"),
        CheckConstraint(
            'length(trim(statement))>0 AND length(trim(verification_method))>0 AND "order">=0'
        ),
    )


class CompletionCriteriaHead(Base):
    __tablename__ = "completion_criteria_heads"
    enrollment_id: Mapped[UUID] = mapped_column(
        ForeignKey("completion_enrollments.id", ondelete="RESTRICT"), primary_key=True
    )
    generation: Mapped[int] = mapped_column(Integer, nullable=False)
    revision_id: Mapped[UUID] = mapped_column(nullable=False)
    __table_args__ = (
        ForeignKeyConstraint(
            ["enrollment_id", "revision_id"],
            [
                "completion_criteria_revisions.enrollment_id",
                "completion_criteria_revisions.id",
            ],
            ondelete="RESTRICT",
        ),
        CheckConstraint("generation>0"),
    )


class CompletionReviewPolicy(Base):
    __tablename__ = "completion_review_policies"
    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    scope_type: Mapped[str] = mapped_column(String(12), nullable=False)
    scope_id: Mapped[UUID] = mapped_column(nullable=False)
    project_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("projects.id", ondelete="RESTRICT")
    )
    work_order_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("work_order.id", ondelete="RESTRICT")
    )
    revision: Mapped[int] = mapped_column(Integer, nullable=False)
    independent_reviewer_required: Mapped[bool] = mapped_column(Boolean, nullable=False)
    actor_user_id: Mapped[UUID] = mapped_column(
        ForeignKey("system_users.id", ondelete="RESTRICT"), nullable=False
    )
    actor_person_id: Mapped[UUID] = mapped_column(
        ForeignKey("parties.id", ondelete="RESTRICT"), nullable=False
    )
    reason: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(UTC), nullable=False
    )
    __table_args__ = (
        UniqueConstraint("scope_type", "scope_id", "revision"),
        UniqueConstraint("scope_type", "scope_id", "id"),
        CheckConstraint(
            "(scope_type='project' AND project_id IS NOT NULL AND scope_id=project_id AND work_order_id IS NULL) OR (scope_type='work_order' AND work_order_id IS NOT NULL AND scope_id=work_order_id AND project_id IS NULL)"
        ),
        CheckConstraint("revision>0 AND length(trim(reason))>0"),
    )


class CompletionReviewPolicyHead(Base):
    __tablename__ = "completion_review_policy_heads"
    scope_type: Mapped[str] = mapped_column(String(12), primary_key=True)
    scope_id: Mapped[UUID] = mapped_column(primary_key=True)
    generation: Mapped[int] = mapped_column(Integer, nullable=False)
    revision_id: Mapped[UUID] = mapped_column(nullable=False)
    __table_args__ = (
        ForeignKeyConstraint(
            ["scope_type", "scope_id", "revision_id"],
            [
                "completion_review_policies.scope_type",
                "completion_review_policies.scope_id",
                "completion_review_policies.id",
            ],
            ondelete="RESTRICT",
        ),
        CheckConstraint("generation>0"),
    )


class CompletionSubmission(Base):
    __tablename__ = "completion_submissions"
    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    enrollment_id: Mapped[UUID] = mapped_column(
        ForeignKey("completion_enrollments.id", ondelete="RESTRICT"),
        nullable=False,
    )
    policy_revision_id: Mapped[UUID] = mapped_column(
        ForeignKey("completion_review_policies.id", ondelete="RESTRICT"), nullable=False
    )
    criteria_revision_id: Mapped[UUID] = mapped_column(
        ForeignKey("completion_criteria_revisions.id", ondelete="RESTRICT"),
        nullable=False,
    )
    completed_event_id: Mapped[UUID] = mapped_column(
        ForeignKey("field_job_events.id", ondelete="RESTRICT"), nullable=False
    )
    assignment_generation: Mapped[UUID] = mapped_column(nullable=False)
    actor_user_id: Mapped[UUID] = mapped_column(
        ForeignKey("system_users.id", ondelete="RESTRICT"), nullable=False
    )
    actor_person_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("parties.id", ondelete="RESTRICT")
    )
    actor_vendor_user_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("field_vendor_users.id", ondelete="RESTRICT")
    )
    reason: Mapped[str] = mapped_column(Text, nullable=False)
    submitted_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(UTC), nullable=False
    )
    creation_outer_xid: Mapped[str | None] = mapped_column(
        OuterTransactionId(), nullable=True, server_default=FetchedValue()
    )
    __table_args__ = (
        UniqueConstraint("id", "criteria_revision_id"),
        ForeignKeyConstraint(
            ["enrollment_id", "criteria_revision_id"],
            [
                "completion_criteria_revisions.enrollment_id",
                "completion_criteria_revisions.id",
            ],
            ondelete="RESTRICT",
        ),
        UniqueConstraint("enrollment_id", "id"),
        CheckConstraint("length(trim(reason)) > 0"),
    )


class CompletionObservation(Base):
    __tablename__ = "completion_observations"
    submission_id: Mapped[UUID] = mapped_column(primary_key=True)
    criterion_id: Mapped[UUID] = mapped_column(primary_key=True)
    criteria_revision_id: Mapped[UUID] = mapped_column(nullable=False)
    observation: Mapped[str] = mapped_column(Text, nullable=False)
    __table_args__ = (
        ForeignKeyConstraint(
            ["submission_id", "criteria_revision_id"],
            [
                "completion_submissions.id",
                "completion_submissions.criteria_revision_id",
            ],
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["criteria_revision_id", "criterion_id"],
            ["completion_criteria.revision_id", "completion_criteria.id"],
            ondelete="RESTRICT",
        ),
        CheckConstraint("length(trim(observation)) > 0"),
    )


class CompletionEvidenceFile(Base):
    __tablename__ = "completion_evidence_files"
    submission_id: Mapped[UUID] = mapped_column(primary_key=True)
    criterion_id: Mapped[UUID] = mapped_column(primary_key=True)
    attachment_id: Mapped[UUID] = mapped_column(
        ForeignKey("field_attachments.id", ondelete="RESTRICT"), primary_key=True
    )
    stored_file_id: Mapped[UUID] = mapped_column(
        ForeignKey("stored_files.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    checksum: Mapped[str] = mapped_column(String(64), nullable=False)
    __table_args__ = (
        ForeignKeyConstraint(
            ["submission_id", "criterion_id"],
            [
                "completion_observations.submission_id",
                "completion_observations.criterion_id",
            ],
            ondelete="RESTRICT",
        ),
        CheckConstraint("length(checksum) = 64"),
    )


class CompletionReviewDecision(Base):
    __tablename__ = "completion_review_decisions"
    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    enrollment_id: Mapped[UUID] = mapped_column(nullable=False)
    submission_id: Mapped[UUID] = mapped_column(nullable=False)
    policy_revision_id: Mapped[UUID] = mapped_column(
        ForeignKey("completion_review_policies.id", ondelete="RESTRICT"), nullable=False
    )
    state: Mapped[str] = mapped_column(String(12), nullable=False)
    revokes_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("completion_review_decisions.id", ondelete="RESTRICT")
    )
    actor_user_id: Mapped[UUID] = mapped_column(
        ForeignKey("system_users.id", ondelete="RESTRICT"), nullable=False
    )
    actor_person_id: Mapped[UUID] = mapped_column(
        ForeignKey("parties.id", ondelete="RESTRICT"), nullable=False
    )
    reason: Mapped[str] = mapped_column(Text, nullable=False)
    decided_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(UTC), nullable=False
    )
    creation_outer_xid: Mapped[str | None] = mapped_column(
        OuterTransactionId(), nullable=True, server_default=FetchedValue()
    )
    __table_args__ = (
        UniqueConstraint("enrollment_id", "id"),
        ForeignKeyConstraint(
            ["enrollment_id", "submission_id"],
            ["completion_submissions.enrollment_id", "completion_submissions.id"],
            ondelete="RESTRICT",
        ),
        CheckConstraint(
            "state IN ('accepted','rejected','revoked') AND length(trim(reason)) > 0 AND ((state='revoked') = (revokes_id IS NOT NULL))"
        ),
    )


class CompletionCriterionResult(Base):
    __tablename__ = "completion_criterion_results"
    decision_id: Mapped[UUID] = mapped_column(
        ForeignKey("completion_review_decisions.id", ondelete="RESTRICT"),
        primary_key=True,
    )
    criterion_id: Mapped[UUID] = mapped_column(primary_key=True)
    submission_id: Mapped[UUID] = mapped_column(nullable=False)
    verdict: Mapped[str] = mapped_column(String(8), nullable=False)
    reason: Mapped[str] = mapped_column(Text, nullable=False)
    __table_args__ = (
        ForeignKeyConstraint(
            ["submission_id", "criterion_id"],
            [
                "completion_observations.submission_id",
                "completion_observations.criterion_id",
            ],
            ondelete="RESTRICT",
        ),
        CheckConstraint("verdict IN ('passed','failed') AND length(trim(reason)) > 0"),
    )


class CompletionReviewHead(Base):
    __tablename__ = "completion_review_heads"
    enrollment_id: Mapped[UUID] = mapped_column(
        ForeignKey("completion_enrollments.id", ondelete="RESTRICT"),
        primary_key=True,
    )
    generation: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    submission_id: Mapped[UUID] = mapped_column(nullable=False)
    decision_id: Mapped[UUID | None] = mapped_column()
    __table_args__ = (
        ForeignKeyConstraint(
            ["enrollment_id", "submission_id"],
            ["completion_submissions.enrollment_id", "completion_submissions.id"],
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["enrollment_id", "decision_id"],
            [
                "completion_review_decisions.enrollment_id",
                "completion_review_decisions.id",
            ],
            ondelete="RESTRICT",
        ),
        CheckConstraint("generation > 0"),
    )


class CompletionReviewReceipt(Base):
    __tablename__ = "completion_review_receipts"
    idempotency_key: Mapped[str] = mapped_column(String(120), primary_key=True)
    fingerprint: Mapped[str] = mapped_column(String(64), nullable=False)
    enrollment_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("completion_enrollments.id", ondelete="RESTRICT")
    )
    criteria_revision_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("completion_criteria_revisions.id", ondelete="RESTRICT")
    )
    completed_event_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("field_job_events.id", ondelete="RESTRICT")
    )
    criteria_generation: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    policy_revision_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("completion_review_policies.id", ondelete="RESTRICT")
    )
    submission_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("completion_submissions.id", ondelete="RESTRICT")
    )
    decision_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("completion_review_decisions.id", ondelete="RESTRICT")
    )
    generation: Mapped[int] = mapped_column(Integer, nullable=False)
    state: Mapped[str] = mapped_column(String(24), nullable=False)
    __table_args__ = (
        CheckConstraint(
            "length(trim(idempotency_key)) > 0 AND length(fingerprint)=64 AND generation >= 0"
        ),
    )


IMMUTABLE_MODELS = (
    CompletionEnrollment,
    CompletionCriteriaRevision,
    CompletionCriterion,
    CompletionReviewPolicy,
    CompletionSubmission,
    CompletionObservation,
    CompletionEvidenceFile,
    CompletionReviewDecision,
    CompletionCriterionResult,
    CompletionReviewReceipt,
)


def _immutable(_mapper: object, _connection: object, _target: object) -> None:
    raise ValueError("Completion review history is immutable")


for _model in IMMUTABLE_MODELS:
    event.listen(_model, "before_update", _immutable)
    event.listen(_model, "before_delete", _immutable)


class CompletionReplacementDecision(Base):
    __tablename__ = "completion_replacement_decisions"
    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    source_enrollment_id: Mapped[UUID] = mapped_column(
        ForeignKey("completion_enrollments.id", ondelete="RESTRICT"), nullable=False
    )
    replacement_enrollment_id: Mapped[UUID] = mapped_column(
        ForeignKey("completion_enrollments.id", ondelete="RESTRICT"), nullable=False
    )
    generation: Mapped[int] = mapped_column(Integer, nullable=False)
    state: Mapped[str] = mapped_column(String(12), nullable=False)
    revokes_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("completion_replacement_decisions.id", ondelete="RESTRICT")
    )
    source_criteria_revision_id: Mapped[UUID] = mapped_column(
        ForeignKey("completion_criteria_revisions.id", ondelete="RESTRICT"),
        nullable=False,
    )
    replacement_criteria_revision_id: Mapped[UUID] = mapped_column(
        ForeignKey("completion_criteria_revisions.id", ondelete="RESTRICT"),
        nullable=False,
    )
    source_policy_revision_id: Mapped[UUID] = mapped_column(
        ForeignKey("completion_review_policies.id", ondelete="RESTRICT"), nullable=False
    )
    replacement_policy_revision_id: Mapped[UUID] = mapped_column(
        ForeignKey("completion_review_policies.id", ondelete="RESTRICT"), nullable=False
    )
    source_assignment_generation: Mapped[UUID] = mapped_column(nullable=False)
    replacement_assignment_generation: Mapped[UUID] = mapped_column(nullable=False)
    failure_event_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("field_job_events.id", ondelete="RESTRICT")
    )
    failure_submission_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("completion_submissions.id", ondelete="RESTRICT")
    )
    failure_decision_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("completion_review_decisions.id", ondelete="RESTRICT")
    )
    failure_review_generation: Mapped[int | None] = mapped_column(Integer)
    actor_user_id: Mapped[UUID] = mapped_column(
        ForeignKey("system_users.id", ondelete="RESTRICT"), nullable=False
    )
    actor_person_id: Mapped[UUID] = mapped_column(
        ForeignKey("parties.id", ondelete="RESTRICT"), nullable=False
    )
    reason: Mapped[str] = mapped_column(Text, nullable=False)
    decided_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(UTC), nullable=False
    )
    __table_args__ = (
        UniqueConstraint("source_enrollment_id", "generation"),
        CheckConstraint(
            "source_enrollment_id <> replacement_enrollment_id AND generation > 0 AND length(trim(reason)) > 0"
        ),
        CheckConstraint(
            "state IN ('approved','revoked') AND ((state='revoked') = (revokes_id IS NOT NULL))"
        ),
        CheckConstraint(
            "(failure_event_id IS NOT NULL AND failure_submission_id IS NULL AND failure_decision_id IS NULL AND failure_review_generation IS NULL) OR (failure_event_id IS NULL AND failure_submission_id IS NOT NULL AND failure_decision_id IS NOT NULL AND failure_review_generation > 0)"
        ),
    )
    creation_outer_xid: Mapped[str | None] = mapped_column(
        OuterTransactionId(), nullable=True, server_default=FetchedValue()
    )


class CompletionReplacementCoverage(Base):
    __tablename__ = "completion_replacement_coverage"
    decision_id: Mapped[UUID] = mapped_column(
        ForeignKey("completion_replacement_decisions.id", ondelete="RESTRICT"),
        primary_key=True,
    )
    source_criterion_id: Mapped[UUID] = mapped_column(primary_key=True)
    replacement_criterion_id: Mapped[UUID] = mapped_column(nullable=False)

    __table_args__ = (UniqueConstraint("decision_id", "replacement_criterion_id"),)


class CompletionReplacementHead(Base):
    __tablename__ = "completion_replacement_heads"
    source_enrollment_id: Mapped[UUID] = mapped_column(
        ForeignKey("completion_enrollments.id", ondelete="RESTRICT"), primary_key=True
    )
    replacement_enrollment_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("completion_enrollments.id", ondelete="RESTRICT"), unique=True
    )
    decision_id: Mapped[UUID] = mapped_column(
        ForeignKey("completion_replacement_decisions.id", ondelete="RESTRICT"),
        nullable=False,
    )
    generation: Mapped[int] = mapped_column(Integer, nullable=False)
    __table_args__ = (
        CheckConstraint(
            "generation > 0 AND source_enrollment_id <> replacement_enrollment_id"
        ),
    )


class CompletionReplacementReceipt(Base):
    __tablename__ = "completion_replacement_receipts"
    idempotency_key: Mapped[str] = mapped_column(String(120), primary_key=True)
    fingerprint: Mapped[str] = mapped_column(String(64), nullable=False)
    decision_id: Mapped[UUID] = mapped_column(
        ForeignKey("completion_replacement_decisions.id", ondelete="RESTRICT"),
        nullable=False,
    )
    __table_args__ = (
        CheckConstraint(
            "length(trim(idempotency_key)) > 0 AND length(fingerprint) = 64"
        ),
    )


for _replacement_model in (
    CompletionReplacementDecision,
    CompletionReplacementCoverage,
    CompletionReplacementReceipt,
):
    event.listen(_replacement_model, "before_update", _immutable)
    event.listen(_replacement_model, "before_delete", _immutable)
