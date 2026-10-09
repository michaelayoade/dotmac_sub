"""Immutable recipient snapshot for one workflow's Finance review action."""

import uuid
from datetime import UTC, datetime

from sqlalchemy import (
    JSON,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Integer,
    String,
    UniqueConstraint,
)
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db import Base


class TestConnectionFinanceReview(Base):
    __tablename__ = "test_connection_finance_reviews"
    __table_args__ = (
        CheckConstraint("step_index >= 0", name="ck_test_connection_review_step"),
        UniqueConstraint(
            "event_id",
            "rule_version_id",
            "step_index",
            name="uq_test_connection_review_step",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True)
    event_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("event_store.event_id", ondelete="RESTRICT"),
        nullable=False,
    )
    rule_version_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("automation_rule_versions.id", ondelete="RESTRICT"),
        nullable=False,
    )
    step_index: Mapped[int] = mapped_column(Integer, nullable=False)
    service_team_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("service_teams.id", ondelete="RESTRICT"),
        nullable=False,
    )
    recipient_ids: Mapped[list[str]] = mapped_column(JSON, nullable=False)
    payload_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(UTC), nullable=False
    )
