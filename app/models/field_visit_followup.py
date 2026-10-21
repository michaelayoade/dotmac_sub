"""Native accountable followups with immutable failed-source and review history."""

from datetime import UTC, datetime
from uuid import UUID, uuid4

from sqlalchemy import (
    CheckConstraint,
    DateTime,
    ForeignKey,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.db import Base


class FieldVisitFollowup(Base):
    __tablename__ = "field_visit_followups"
    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    work_order_id: Mapped[UUID] = mapped_column(
        ForeignKey("work_order.id", ondelete="RESTRICT"), nullable=False
    )
    field_event_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("field_job_events.id", ondelete="RESTRICT"), unique=True
    )
    rejected_decision_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("completion_review_decisions.id", ondelete="RESTRICT"), unique=True
    )
    current_sequence: Mapped[int] = mapped_column(Integer, nullable=False)
    __table_args__ = (
        CheckConstraint(
            "(field_event_id IS NOT NULL) <> (rejected_decision_id IS NOT NULL)"
        ),
        CheckConstraint("current_sequence > 0"),
    )


class FieldVisitFollowupRevision(Base):
    __tablename__ = "field_visit_followup_revisions"
    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    obligation_id: Mapped[UUID] = mapped_column(
        ForeignKey("field_visit_followups.id", ondelete="RESTRICT"), nullable=False
    )
    sequence: Mapped[int] = mapped_column(Integer, nullable=False)
    disposition: Mapped[str] = mapped_column(String(24), nullable=False)
    responsible_team_id: Mapped[UUID] = mapped_column(
        ForeignKey("service_teams.id", ondelete="RESTRICT"), nullable=False
    )
    responsible_system_user_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("system_users.id", ondelete="RESTRICT")
    )
    responsible_person_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("parties.id", ondelete="RESTRICT")
    )
    due_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    reason: Mapped[str] = mapped_column(Text, nullable=False)
    replacement_decision_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("completion_replacement_decisions.id", ondelete="RESTRICT")
    )
    actor_user_id: Mapped[UUID] = mapped_column(
        ForeignKey("system_users.id", ondelete="RESTRICT"), nullable=False
    )
    actor_person_id: Mapped[UUID] = mapped_column(
        ForeignKey("parties.id", ondelete="RESTRICT"), nullable=False
    )
    decided_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(UTC), nullable=False
    )
    __table_args__ = (
        UniqueConstraint("obligation_id", "sequence"),
        CheckConstraint("sequence > 0"),
        CheckConstraint("disposition IN ('triage','revisit_required','no_revisit')"),
        CheckConstraint("length(trim(reason)) > 0"),
        CheckConstraint(
            "(responsible_system_user_id IS NULL) = (responsible_person_id IS NULL)"
        ),
        CheckConstraint(
            "replacement_decision_id IS NULL OR disposition = 'revisit_required'"
        ),
    )


class FieldVisitFollowupReceipt(Base):
    __tablename__ = "field_visit_followup_receipts"
    intent_key: Mapped[UUID] = mapped_column(primary_key=True)
    fingerprint: Mapped[str] = mapped_column(String(64), nullable=False)
    revision_id: Mapped[UUID] = mapped_column(
        ForeignKey("field_visit_followup_revisions.id", ondelete="RESTRICT"),
        nullable=False,
    )
    __table_args__ = (CheckConstraint("length(fingerprint) = 64"),)
