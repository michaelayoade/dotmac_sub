"""Project-owned physical requirement markers and immutable applied evidence."""

from datetime import UTC, datetime
from uuid import UUID, uuid4

from sqlalchemy import (
    CheckConstraint,
    FetchedValue,
    ForeignKey,
    ForeignKeyConstraint,
    String,
    Text,
    UniqueConstraint,
    event,
)
from sqlalchemy.orm import Mapped, mapped_column
from sqlalchemy.types import DateTime

from app.db import Base
from app.models.transaction_provenance import OuterTransactionId


class ProjectFieldCompletionRequirement(Base):
    __tablename__ = "project_field_completion_requirements"
    enrollment_id: Mapped[UUID] = mapped_column(
        ForeignKey("completion_enrollments.id", ondelete="RESTRICT"), primary_key=True
    )
    work_order_id: Mapped[UUID] = mapped_column(
        ForeignKey("work_order.id", ondelete="RESTRICT"), nullable=False, unique=True
    )
    project_id: Mapped[UUID] = mapped_column(
        ForeignKey("projects.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    task_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("project_tasks.id", ondelete="RESTRICT"), index=True
    )
    project_subscriber_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("subscribers.id", ondelete="RESTRICT")
    )
    registered_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(UTC), nullable=False
    )
    __table_args__ = (
        ForeignKeyConstraint(
            ["project_id", "task_id"],
            ["project_tasks.project_id", "project_tasks.id"],
            ondelete="RESTRICT",
        ),
    )


class ProjectFieldCompletionApplication(Base):
    __tablename__ = "project_field_completion_applications"
    creation_outer_xid: Mapped[str | None] = mapped_column(
        OuterTransactionId(), nullable=True, server_default=FetchedValue()
    )
    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    project_id: Mapped[UUID] = mapped_column(
        ForeignKey("projects.id", ondelete="RESTRICT"), nullable=False
    )
    task_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("project_tasks.id", ondelete="RESTRICT")
    )
    expected_project_revision: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    expected_task_revision: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True)
    )
    requirement_token: Mapped[str] = mapped_column(String(64), nullable=False)
    actor_user_id: Mapped[UUID] = mapped_column(
        ForeignKey("system_users.id", ondelete="RESTRICT"), nullable=False
    )
    actor_person_id: Mapped[UUID] = mapped_column(
        ForeignKey("parties.id", ondelete="RESTRICT"), nullable=False
    )
    intent_id: Mapped[UUID] = mapped_column(nullable=False)
    command_id: Mapped[UUID] = mapped_column(nullable=False)
    reason: Mapped[str] = mapped_column(Text, nullable=False)
    applied_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(UTC), nullable=False
    )
    __table_args__ = (
        CheckConstraint(
            "length(requirement_token)=64 AND length(trim(reason))>0 AND ((task_id IS NULL) = (expected_task_revision IS NULL))"
        ),
        UniqueConstraint("id", "intent_id"),
        ForeignKeyConstraint(
            ["project_id", "task_id"],
            ["project_tasks.project_id", "project_tasks.id"],
            ondelete="RESTRICT",
        ),
    )


class ProjectFieldCompletionBasis(Base):
    __tablename__ = "project_field_completion_bases"
    application_id: Mapped[UUID] = mapped_column(
        ForeignKey("project_field_completion_applications.id", ondelete="RESTRICT"),
        primary_key=True,
    )
    enrollment_id: Mapped[UUID] = mapped_column(
        ForeignKey(
            "project_field_completion_requirements.enrollment_id", ondelete="RESTRICT"
        ),
        primary_key=True,
    )
    work_order_id: Mapped[UUID] = mapped_column(
        ForeignKey("work_order.id", ondelete="RESTRICT"), nullable=False
    )
    effective_enrollment_id: Mapped[UUID] = mapped_column(
        ForeignKey("completion_enrollments.id", ondelete="RESTRICT"), nullable=False
    )
    effective_work_order_id: Mapped[UUID] = mapped_column(
        ForeignKey("work_order.id", ondelete="RESTRICT"), nullable=False
    )
    criteria_revision_id: Mapped[UUID] = mapped_column(
        ForeignKey("completion_criteria_revisions.id", ondelete="RESTRICT"),
        nullable=False,
    )
    policy_revision_id: Mapped[UUID] = mapped_column(
        ForeignKey("completion_review_policies.id", ondelete="RESTRICT"),
        nullable=False,
    )
    submission_id: Mapped[UUID] = mapped_column(
        ForeignKey("completion_submissions.id", ondelete="RESTRICT"), nullable=False
    )
    accepted_decision_id: Mapped[UUID] = mapped_column(
        ForeignKey("completion_review_decisions.id", ondelete="RESTRICT"),
        nullable=False,
    )
    review_generation: Mapped[int] = mapped_column(nullable=False)
    completed_event_id: Mapped[UUID] = mapped_column(
        ForeignKey("field_job_events.id", ondelete="RESTRICT"), nullable=False
    )
    assignment_generation: Mapped[UUID] = mapped_column(nullable=False)


class ProjectFieldCompletionLineage(Base):
    __tablename__ = "project_field_completion_lineage"
    application_id: Mapped[UUID] = mapped_column(primary_key=True)
    enrollment_id: Mapped[UUID] = mapped_column(primary_key=True)
    position: Mapped[int] = mapped_column(primary_key=True)
    decision_id: Mapped[UUID] = mapped_column(
        ForeignKey("completion_replacement_decisions.id", ondelete="RESTRICT"),
        nullable=False,
    )
    __table_args__ = (
        ForeignKeyConstraint(
            ["application_id", "enrollment_id"],
            [
                "project_field_completion_bases.application_id",
                "project_field_completion_bases.enrollment_id",
            ],
            ondelete="RESTRICT",
        ),
        CheckConstraint("position>=0"),
        UniqueConstraint("application_id", "enrollment_id", "decision_id"),
    )


class ProjectFieldCompletionReceipt(Base):
    __tablename__ = "project_field_completion_receipts"
    intent_id: Mapped[UUID] = mapped_column(primary_key=True)
    fingerprint: Mapped[str] = mapped_column(String(64), nullable=False)
    application_id: Mapped[UUID] = mapped_column(
        ForeignKey("project_field_completion_applications.id", ondelete="RESTRICT"),
        nullable=False,
        unique=True,
    )
    __table_args__ = (
        CheckConstraint("length(fingerprint)=64"),
        ForeignKeyConstraint(
            ["application_id", "intent_id"],
            [
                "project_field_completion_applications.id",
                "project_field_completion_applications.intent_id",
            ],
            ondelete="RESTRICT",
        ),
    )


def _immutable(_mapper: object, _connection: object, _target: object) -> None:
    raise ValueError("Applied project field completion evidence is immutable")


for _model in (
    ProjectFieldCompletionRequirement,
    ProjectFieldCompletionApplication,
    ProjectFieldCompletionBasis,
    ProjectFieldCompletionLineage,
    ProjectFieldCompletionReceipt,
):
    event.listen(_model, "before_update", _immutable)
    event.listen(_model, "before_delete", _immutable)
