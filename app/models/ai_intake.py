from __future__ import annotations

import uuid
from datetime import UTC, datetime

from sqlalchemy import (
    JSON,
    Boolean,
    CheckConstraint,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.ext.mutable import MutableDict, MutableList
from sqlalchemy.orm import Mapped, mapped_column

from app.db import Base


class AiIntakeConfig(Base):
    __tablename__ = "ai_intake_configs"
    __table_args__ = (
        UniqueConstraint("scope_key", name="uq_ai_intake_configs_scope_key"),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    scope_key: Mapped[str] = mapped_column(String(160), nullable=False)
    channel_type: Mapped[str] = mapped_column(String(40), nullable=False)
    is_enabled: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    confidence_threshold: Mapped[float] = mapped_column(
        Float, default=0.75, nullable=False
    )
    allow_followup_questions: Mapped[bool] = mapped_column(
        Boolean, default=True, nullable=False
    )
    max_clarification_turns: Mapped[int] = mapped_column(
        Integer, default=1, nullable=False
    )
    escalate_after_minutes: Mapped[int] = mapped_column(
        Integer, default=5, nullable=False
    )
    exclude_campaign_attribution: Mapped[bool] = mapped_column(
        Boolean, default=True, nullable=False
    )
    fallback_team_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    instructions: Mapped[str | None] = mapped_column(Text)
    department_mappings: Mapped[list | None] = mapped_column(
        MutableList.as_mutable(JSON())
    )
    metadata_: Mapped[dict | None] = mapped_column(
        "metadata", MutableDict.as_mutable(JSON())
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(UTC)
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(UTC),
        onupdate=lambda: datetime.now(UTC),
    )


class CustomerAiIntakeAssessment(Base):
    """One durable, idempotent general-intake decision per inbound message."""

    __tablename__ = "customer_ai_intake_assessments"
    __table_args__ = (
        UniqueConstraint(
            "message_id", name="uq_customer_ai_intake_assessments_message"
        ),
        CheckConstraint(
            "confidence >= 0 AND confidence <= 1",
            name="ck_customer_ai_intake_assessments_confidence",
        ),
        CheckConstraint(
            "follow_up_turn >= 0", name="ck_customer_ai_intake_follow_up_turn"
        ),
        Index(
            "ix_customer_ai_intake_conversation_created",
            "conversation_id",
            "created_at",
        ),
        Index(
            "ix_customer_ai_intake_fallback_due",
            "status",
            "fallback_due_at",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    config_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("ai_intake_configs.id", ondelete="SET NULL")
    )
    conversation_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("inbox_conversations.id", ondelete="CASCADE"),
        nullable=False,
    )
    message_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("inbox_messages.id", ondelete="CASCADE"),
        nullable=False,
    )
    channel_type: Mapped[str] = mapped_column(String(40), nullable=False)
    intent_key: Mapped[str] = mapped_column(String(80), nullable=False)
    category_key: Mapped[str] = mapped_column(String(80), nullable=False)
    confidence: Mapped[float] = mapped_column(Float, nullable=False)
    department_key: Mapped[str] = mapped_column(String(80), nullable=False)
    status: Mapped[str] = mapped_column(String(40), nullable=False)
    requires_follow_up: Mapped[bool] = mapped_column(
        Boolean, default=False, nullable=False
    )
    follow_up_question_key: Mapped[str | None] = mapped_column(String(80))
    follow_up_question: Mapped[str | None] = mapped_column(String(320))
    follow_up_turn: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    summary: Mapped[str | None] = mapped_column(String(500))
    destination_team_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("service_teams.id")
    )
    route_reason: Mapped[str | None] = mapped_column(String(80))
    provider_label: Mapped[str | None] = mapped_column(String(80))
    model_label: Mapped[str | None] = mapped_column(String(160))
    failure_code: Mapped[str | None] = mapped_column(String(120))
    fallback_due_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(UTC), nullable=False
    )
