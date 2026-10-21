"""Immutable project issuance provenance and historical receipt, never approval."""

from datetime import UTC, datetime
from uuid import UUID, uuid4

from sqlalchemy import (
    CheckConstraint,
    ForeignKey,
    ForeignKeyConstraint,
    String,
    Text,
    event,
)
from sqlalchemy.orm import Mapped, mapped_column
from sqlalchemy.types import DateTime

from app.db import Base


class ProjectWorkOrderIssuance(Base):
    __tablename__ = "project_work_order_issuances"
    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    idempotency_key: Mapped[str] = mapped_column(
        String(120), unique=True, nullable=False
    )
    fingerprint: Mapped[str] = mapped_column(String(64), nullable=False)
    work_order_id: Mapped[UUID] = mapped_column(
        ForeignKey("work_order.id", ondelete="RESTRICT"), unique=True, nullable=False
    )
    public_id: Mapped[str] = mapped_column(String(64), nullable=False)
    project_id: Mapped[UUID] = mapped_column(
        ForeignKey("projects.id", ondelete="RESTRICT"), nullable=False
    )
    task_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("project_tasks.id", ondelete="RESTRICT")
    )
    plan_id: Mapped[UUID] = mapped_column(nullable=False)
    revision_id: Mapped[UUID] = mapped_column(nullable=False)
    approval_id: Mapped[UUID] = mapped_column(nullable=False)
    digest: Mapped[str] = mapped_column(String(64), nullable=False)
    infrastructure_type: Mapped[str] = mapped_column(String(40), nullable=False)
    infrastructure_id: Mapped[UUID] = mapped_column(nullable=False)
    purpose: Mapped[str] = mapped_column(String(40), nullable=False)
    technology: Mapped[str] = mapped_column(String(10), nullable=False)
    actor_user_id: Mapped[UUID] = mapped_column(
        ForeignKey("system_users.id", ondelete="RESTRICT"), nullable=False
    )
    actor_person_id: Mapped[UUID] = mapped_column(
        ForeignKey("parties.id", ondelete="RESTRICT"), nullable=False
    )
    reason: Mapped[str] = mapped_column(Text, nullable=False)
    command_id: Mapped[UUID] = mapped_column(nullable=False)
    issued_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(UTC), nullable=False
    )
    __table_args__ = (
        ForeignKeyConstraint(
            ["project_id", "task_id"],
            ["project_tasks.project_id", "project_tasks.id"],
            ondelete="RESTRICT",
            name="fk_project_issuance_task",
        ),
        ForeignKeyConstraint(
            ["plan_id", "project_id"],
            ["project_engineering_plans.id", "project_engineering_plans.project_id"],
            ondelete="RESTRICT",
            name="fk_project_issuance_plan",
        ),
        ForeignKeyConstraint(
            ["plan_id", "approval_id", "revision_id"],
            [
                "engineering_plan_decisions.plan_id",
                "engineering_plan_decisions.id",
                "engineering_plan_decisions.revision_id",
            ],
            ondelete="RESTRICT",
            name="fk_project_issuance_approval",
        ),
        CheckConstraint(
            "length(trim(idempotency_key)) > 0 AND length(fingerprint) = 64 AND length(digest) = 64 AND length(trim(reason)) > 0 AND technology IN ('fiber','radio')",
            name="ck_project_issuance",
        ),
    )


def _immutable(_mapper: object, _connection: object, _target: object) -> None:
    raise ValueError("Project issuance evidence is immutable")


event.listen(ProjectWorkOrderIssuance, "before_update", _immutable)
event.listen(ProjectWorkOrderIssuance, "before_delete", _immutable)
