"""Native immutable engineering evidence and mutable generation-controlled heads."""

from datetime import UTC, datetime
from uuid import UUID, uuid4

from sqlalchemy import (
    JSON,
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    ForeignKeyConstraint,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    event,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.db import Base


class EngineeringPolicyHead(Base):
    __tablename__ = "engineering_policy_head"
    project_id: Mapped[UUID] = mapped_column(
        ForeignKey("projects.id", ondelete="RESTRICT"), primary_key=True
    )
    generation: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    current_revision_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("engineering_policy_revisions.id", ondelete="RESTRICT")
    )
    __table_args__ = (
        CheckConstraint("generation >= 0", name="ck_engineering_policy_head"),
        ForeignKeyConstraint(
            ["project_id", "current_revision_id"],
            [
                "engineering_policy_revisions.project_id",
                "engineering_policy_revisions.id",
            ],
            ondelete="RESTRICT",
            name="fk_engineering_policy_head_revision",
        ),
    )


class EngineeringPolicyRevision(Base):
    __tablename__ = "engineering_policy_revisions"
    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    project_id: Mapped[UUID] = mapped_column(
        ForeignKey("projects.id", ondelete="RESTRICT"), nullable=False
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
    command_id: Mapped[UUID] = mapped_column(nullable=False, unique=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=lambda: datetime.now(UTC)
    )
    __table_args__ = (
        CheckConstraint(
            "revision > 0 AND length(trim(reason)) > 0",
            name="ck_engineering_policy_revision",
        ),
        UniqueConstraint(
            "project_id", "revision", name="uq_engineering_policy_project_revision"
        ),
        UniqueConstraint(
            "project_id", "id", name="uq_engineering_policy_revision_pair"
        ),
    )


class ProjectEngineeringPlan(Base):
    __tablename__ = "project_engineering_plans"
    id: Mapped[UUID] = mapped_column(primary_key=True)
    project_id: Mapped[UUID] = mapped_column(
        ForeignKey("projects.id", ondelete="RESTRICT"), nullable=False
    )
    task_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("project_tasks.id", ondelete="RESTRICT")
    )
    technology: Mapped[str] = mapped_column(String(10), nullable=False)
    generation: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    latest_revision_id: Mapped[UUID | None] = mapped_column()
    current_approval_id: Mapped[UUID | None] = mapped_column()
    __table_args__ = (
        UniqueConstraint("id", "project_id", name="uq_engineering_plan_project"),
        ForeignKeyConstraint(
            ["project_id", "task_id"],
            ["project_tasks.project_id", "project_tasks.id"],
            ondelete="RESTRICT",
            name="fk_engineering_plan_task_project",
        ),
        ForeignKeyConstraint(
            ["id", "latest_revision_id"],
            ["engineering_plan_revisions.plan_id", "engineering_plan_revisions.id"],
            name="fk_engineering_latest_revision",
            use_alter=True,
        ),
        ForeignKeyConstraint(
            ["id", "current_approval_id"],
            ["engineering_plan_decisions.plan_id", "engineering_plan_decisions.id"],
            name="fk_engineering_current_approval",
            use_alter=True,
        ),
        CheckConstraint(
            "generation >= 0 AND technology IN ('fiber','radio')",
            name="ck_engineering_plan_head",
        ),
        Index(
            "uq_engineering_project_plan",
            "project_id",
            "technology",
            unique=True,
            postgresql_where=text("task_id IS NULL"),
            sqlite_where=text("task_id IS NULL"),
        ),
        Index(
            "uq_engineering_task_plan",
            "project_id",
            "task_id",
            "technology",
            unique=True,
            postgresql_where=text("task_id IS NOT NULL"),
            sqlite_where=text("task_id IS NOT NULL"),
        ),
    )


class EngineeringPlanRevision(Base):
    __tablename__ = "engineering_plan_revisions"
    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    plan_id: Mapped[UUID] = mapped_column(
        ForeignKey("project_engineering_plans.id", ondelete="RESTRICT"), nullable=False
    )
    revision: Mapped[int] = mapped_column(Integer, nullable=False)
    content: Mapped[dict] = mapped_column(JSON, nullable=False)
    origin_binding: Mapped[dict] = mapped_column(JSON, nullable=False)
    digest: Mapped[str] = mapped_column(String(64), nullable=False)
    author_user_id: Mapped[UUID] = mapped_column(
        ForeignKey("system_users.id", ondelete="RESTRICT"), nullable=False
    )
    author_person_id: Mapped[UUID] = mapped_column(
        ForeignKey("parties.id", ondelete="RESTRICT"), nullable=False
    )
    command_id: Mapped[UUID] = mapped_column(nullable=False, unique=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=lambda: datetime.now(UTC)
    )
    __table_args__ = (
        UniqueConstraint("plan_id", "revision", name="uq_engineering_plan_revision"),
        UniqueConstraint("plan_id", "id", name="uq_engineering_revision_pair"),
        CheckConstraint(
            "revision > 0 AND length(digest) = 64", name="ck_engineering_revision"
        ),
    )


class EngineeringPlanCriterion(Base):
    __tablename__ = "engineering_plan_criteria"
    revision_id: Mapped[UUID] = mapped_column(
        ForeignKey("engineering_plan_revisions.id", ondelete="RESTRICT"),
        primary_key=True,
    )
    id: Mapped[UUID] = mapped_column(primary_key=True)
    order: Mapped[int] = mapped_column(Integer, nullable=False)
    statement: Mapped[str] = mapped_column(Text, nullable=False)
    verification_method: Mapped[str] = mapped_column(Text, nullable=False)
    __table_args__ = (
        UniqueConstraint("revision_id", "order", name="uq_engineering_criterion_order"),
        CheckConstraint(
            '"order" >= 0 AND length(trim(statement)) > 0 AND length(trim(verification_method)) > 0',
            name="ck_engineering_criterion",
        ),
    )


class EngineeringPlanDecision(Base):
    __tablename__ = "engineering_plan_decisions"
    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    plan_id: Mapped[UUID] = mapped_column(
        ForeignKey("project_engineering_plans.id", ondelete="RESTRICT"), nullable=False
    )
    project_id: Mapped[UUID] = mapped_column(nullable=False)
    revision_id: Mapped[UUID] = mapped_column(nullable=False)
    policy_revision_id: Mapped[UUID] = mapped_column(
        ForeignKey("engineering_policy_revisions.id", ondelete="RESTRICT"),
        nullable=False,
    )
    kind: Mapped[str] = mapped_column(String(12), nullable=False)
    digest: Mapped[str] = mapped_column(String(64), nullable=False)
    prior_approval_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("engineering_plan_decisions.id", ondelete="RESTRICT")
    )
    actor_user_id: Mapped[UUID] = mapped_column(
        ForeignKey("system_users.id", ondelete="RESTRICT"), nullable=False
    )
    actor_person_id: Mapped[UUID] = mapped_column(
        ForeignKey("parties.id", ondelete="RESTRICT"), nullable=False
    )
    author_person_id: Mapped[UUID] = mapped_column(
        ForeignKey("parties.id", ondelete="RESTRICT"), nullable=False
    )
    reason: Mapped[str] = mapped_column(Text, nullable=False)
    command_id: Mapped[UUID] = mapped_column(nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=lambda: datetime.now(UTC)
    )
    __table_args__ = (
        UniqueConstraint(
            "plan_id", "id", "revision_id", name="uq_engineering_decision_revision_pair"
        ),
        ForeignKeyConstraint(
            ["plan_id", "project_id"],
            ["project_engineering_plans.id", "project_engineering_plans.project_id"],
            ondelete="RESTRICT",
            name="fk_engineering_decision_project",
        ),
        ForeignKeyConstraint(
            ["project_id", "policy_revision_id"],
            [
                "engineering_policy_revisions.project_id",
                "engineering_policy_revisions.id",
            ],
            ondelete="RESTRICT",
            name="fk_engineering_decision_project_policy",
        ),
        ForeignKeyConstraint(
            ["plan_id", "prior_approval_id"],
            ["engineering_plan_decisions.plan_id", "engineering_plan_decisions.id"],
            ondelete="RESTRICT",
            name="fk_engineering_prior_approval_plan",
        ),
        UniqueConstraint("plan_id", "id", name="uq_engineering_decision_pair"),
        ForeignKeyConstraint(
            ["plan_id", "revision_id"],
            ["engineering_plan_revisions.plan_id", "engineering_plan_revisions.id"],
            ondelete="RESTRICT",
            name="fk_engineering_decision_revision",
        ),
        CheckConstraint(
            "kind IN ('approve','reject','revoke','supersede') AND length(trim(reason)) > 0 AND length(digest) = 64",
            name="ck_engineering_decision",
        ),
    )


class EngineeringPlanReceipt(Base):
    __tablename__ = "engineering_plan_receipts"
    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    idempotency_key: Mapped[str] = mapped_column(
        String(120), nullable=False, unique=True
    )
    fingerprint: Mapped[str] = mapped_column(String(64), nullable=False)
    plan_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("project_engineering_plans.id", ondelete="RESTRICT")
    )
    revision_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("engineering_plan_revisions.id", ondelete="RESTRICT")
    )
    decision_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("engineering_plan_decisions.id", ondelete="RESTRICT")
    )
    policy_revision_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("engineering_policy_revisions.id", ondelete="RESTRICT")
    )
    generation: Mapped[int] = mapped_column(Integer, nullable=False)
    action: Mapped[str] = mapped_column(String(20), nullable=False)
    digest: Mapped[str | None] = mapped_column(String(64))
    __table_args__ = (
        ForeignKeyConstraint(
            ["plan_id", "decision_id", "revision_id"],
            [
                "engineering_plan_decisions.plan_id",
                "engineering_plan_decisions.id",
                "engineering_plan_decisions.revision_id",
            ],
            ondelete="RESTRICT",
            name="fk_engineering_receipt_decision_revision",
        ),
        ForeignKeyConstraint(
            ["plan_id", "revision_id"],
            ["engineering_plan_revisions.plan_id", "engineering_plan_revisions.id"],
            ondelete="RESTRICT",
            name="fk_engineering_receipt_revision",
        ),
        ForeignKeyConstraint(
            ["plan_id", "decision_id"],
            ["engineering_plan_decisions.plan_id", "engineering_plan_decisions.id"],
            ondelete="RESTRICT",
            name="fk_engineering_receipt_decision",
        ),
        CheckConstraint(
            "generation > 0 AND length(trim(idempotency_key)) > 0 AND length(fingerprint) = 64",
            name="ck_engineering_receipt",
        ),
    )


def _immutable(_mapper: object, _connection: object, _target: object) -> None:
    raise ValueError("Engineering plan evidence is immutable")


for _model in (
    EngineeringPolicyRevision,
    EngineeringPlanRevision,
    EngineeringPlanCriterion,
    EngineeringPlanDecision,
    EngineeringPlanReceipt,
):
    event.listen(_model, "before_update", _immutable)
    event.listen(_model, "before_delete", _immutable)
