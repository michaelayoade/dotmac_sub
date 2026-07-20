from __future__ import annotations

import enum
import uuid
from datetime import UTC, datetime

from sqlalchemy import JSON, DateTime, ForeignKey, Index, Integer, Numeric, String, Text
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.ext.mutable import MutableDict
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db import Base


class InsightDomain(enum.StrEnum):
    tickets = "tickets"
    inbox = "inbox"
    projects = "projects"
    performance = "performance"
    vendors = "vendors"
    dispatch = "dispatch"
    campaigns = "campaigns"
    customer_success = "customer_success"


class InsightSeverity(enum.StrEnum):
    info = "info"
    suggestion = "suggestion"
    warning = "warning"
    critical = "critical"


class AIInsightStatus(enum.StrEnum):
    pending = "pending"
    completed = "completed"
    failed = "failed"
    skipped = "skipped"
    acknowledged = "acknowledged"
    actioned = "actioned"
    expired = "expired"


class AIInsight(Base):
    __tablename__ = "ai_insights"
    __table_args__ = (
        Index("ix_ai_insights_domain_status", "domain", "status"),
        Index("ix_ai_insights_entity", "entity_type", "entity_id"),
        Index("ix_ai_insights_persona", "persona_key"),
        Index("ix_ai_insights_created", "created_at"),
        Index("ix_ai_insights_severity", "severity"),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    persona_key: Mapped[str] = mapped_column(String(80), nullable=False)
    domain: Mapped[str] = mapped_column(String(80), nullable=False)
    severity: Mapped[str] = mapped_column(
        String(40), default=InsightSeverity.info.value, nullable=False
    )
    status: Mapped[str] = mapped_column(
        String(40), default=AIInsightStatus.pending.value, nullable=False
    )
    entity_type: Mapped[str] = mapped_column(String(80), nullable=False)
    entity_id: Mapped[str | None] = mapped_column(String(120))
    title: Mapped[str] = mapped_column(String(300), nullable=False)
    summary: Mapped[str] = mapped_column(Text, nullable=False)
    structured_output: Mapped[dict | None] = mapped_column(
        MutableDict.as_mutable(JSON())
    )
    confidence_score: Mapped[float | None] = mapped_column(Numeric(3, 2))
    recommendations: Mapped[list | None] = mapped_column(JSON)
    context_quality_score: Mapped[float | None] = mapped_column(Numeric(3, 2))
    llm_provider: Mapped[str] = mapped_column(
        String(40), nullable=False, default="native"
    )
    llm_model: Mapped[str | None] = mapped_column(String(100))
    llm_tokens_in: Mapped[int | None] = mapped_column(Integer)
    llm_tokens_out: Mapped[int | None] = mapped_column(Integer)
    llm_endpoint: Mapped[str | None] = mapped_column(String(20))
    generation_time_ms: Mapped[int | None] = mapped_column(Integer)
    trigger: Mapped[str] = mapped_column(String(40), nullable=False)
    triggered_by_system_user_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("system_users.id")
    )
    acknowledged_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    acknowledged_by_system_user_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("system_users.id")
    )
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
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

    triggered_by = relationship(
        "SystemUser", foreign_keys=[triggered_by_system_user_id]
    )
    acknowledged_by = relationship(
        "SystemUser", foreign_keys=[acknowledged_by_system_user_id]
    )
