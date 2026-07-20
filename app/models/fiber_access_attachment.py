from __future__ import annotations

import uuid
from datetime import UTC, datetime
from decimal import Decimal

from sqlalchemy import (
    JSON,
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    Numeric,
    String,
    Text,
    UniqueConstraint,
    text,
)
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db import Base


class FiberAccessAttachmentDecision(Base):
    """Reviewed mutation of one PON input or ONT output attachment."""

    __tablename__ = "fiber_access_attachment_decisions"
    __table_args__ = (
        Index(
            "uq_fiber_access_attachment_active_subject",
            "attachment_type",
            "subject_id",
            unique=True,
            postgresql_where=text("status IN ('proposed', 'approved')"),
            sqlite_where=text("status IN ('proposed', 'approved')"),
        ),
        Index("ix_fiber_access_attachment_status", "status"),
        UniqueConstraint(
            "decision_sha256",
            name="uq_fiber_access_attachment_decision_sha256",
        ),
        CheckConstraint(
            "attachment_type IN ('pon_input', 'ont_output', 'splitter_cascade')",
            name="ck_fiber_access_attachment_type",
        ),
        CheckConstraint(
            "action IN ('attach', 'detach')",
            name="ck_fiber_access_attachment_action",
        ),
        CheckConstraint(
            "status IN ('proposed', 'approved', 'declined', 'applied', 'closed')",
            name="ck_fiber_access_attachment_status",
        ),
        CheckConstraint(
            "(action = 'attach' AND target_splitter_port_id IS NOT NULL) OR "
            "(action = 'detach' AND target_splitter_port_id IS NULL "
            "AND previous_splitter_port_id IS NOT NULL)",
            name="ck_fiber_access_attachment_target",
        ),
        CheckConstraint(
            "reviewed_by IS NULL OR reviewed_by <> proposed_by",
            name="ck_fiber_access_attachment_separation",
        ),
        CheckConstraint(
            "(attachment_type = 'splitter_cascade' "
            "AND upstream_splitter_id IS NOT NULL "
            "AND splitter_stage IS NOT NULL AND splitter_stage >= 2 "
            "AND cumulative_loss_db IS NOT NULL AND cumulative_loss_db >= 0) OR "
            "(attachment_type <> 'splitter_cascade' "
            "AND upstream_splitter_id IS NULL "
            "AND splitter_stage IS NULL AND cumulative_loss_db IS NULL)",
            name="ck_fiber_access_attachment_cascade_evidence",
        ),
        CheckConstraint(
            "(status = 'proposed' AND reviewed_by IS NULL AND reviewed_at IS NULL) OR "
            "(status <> 'proposed' AND reviewed_by IS NOT NULL AND reviewed_at IS NOT NULL)",
            name="ck_fiber_access_attachment_review_evidence",
        ),
        CheckConstraint(
            "(status IN ('applied', 'closed') AND executed_by IS NOT NULL "
            "AND executed_at IS NOT NULL AND result_payload IS NOT NULL "
            "AND result_sha256 IS NOT NULL) OR "
            "(status NOT IN ('applied', 'closed') AND executed_by IS NULL "
            "AND executed_at IS NULL AND result_payload IS NULL "
            "AND result_sha256 IS NULL)",
            name="ck_fiber_access_attachment_result_evidence",
        ),
        CheckConstraint(
            "length(decision_sha256) = 64",
            name="ck_fiber_access_attachment_decision_sha256",
        ),
        CheckConstraint(
            "result_sha256 IS NULL OR length(result_sha256) = 64",
            name="ck_fiber_access_attachment_result_sha256",
        ),
        Index(
            "uq_fiber_access_attachment_active_target",
            "target_splitter_port_id",
            unique=True,
            postgresql_where=text(
                "status IN ('proposed', 'approved') "
                "AND target_splitter_port_id IS NOT NULL"
            ),
            sqlite_where=text(
                "status IN ('proposed', 'approved') "
                "AND target_splitter_port_id IS NOT NULL"
            ),
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    attachment_type: Mapped[str] = mapped_column(String(20), nullable=False)
    action: Mapped[str] = mapped_column(String(16), nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False)
    subject_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    target_splitter_port_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("splitter_ports.id", ondelete="RESTRICT"),
    )
    previous_splitter_port_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("splitter_ports.id", ondelete="RESTRICT"),
    )
    olt_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("olt_devices.id", ondelete="RESTRICT"),
        nullable=False,
    )
    pon_port_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("pon_ports.id", ondelete="RESTRICT"),
        nullable=False,
    )
    splitter_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("splitters.id", ondelete="RESTRICT"),
        nullable=False,
    )
    upstream_splitter_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("splitters.id", ondelete="RESTRICT"),
    )
    splitter_stage: Mapped[int | None] = mapped_column(Integer)
    cumulative_loss_db: Mapped[Decimal | None] = mapped_column(
        Numeric(10, 3), nullable=True
    )
    reason: Mapped[str] = mapped_column(Text, nullable=False)
    decision_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    proposed_by: Mapped[str] = mapped_column(String(160), nullable=False)
    proposed_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(UTC), nullable=False
    )
    reviewed_by: Mapped[str | None] = mapped_column(String(160))
    review_notes: Mapped[str | None] = mapped_column(Text)
    reviewed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    executed_by: Mapped[str | None] = mapped_column(String(160))
    executed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    closed_reason: Mapped[str | None] = mapped_column(Text)
    result_payload: Mapped[dict | None] = mapped_column(JSON)
    result_sha256: Mapped[str | None] = mapped_column(String(64))


class SplitterCascadeLink(Base):
    """Canonical reviewed optical edge between two directed splitter ports."""

    __tablename__ = "splitter_cascade_links"
    __table_args__ = (
        UniqueConstraint(
            "created_by_decision_id",
            name="uq_splitter_cascade_links_create_decision",
        ),
        UniqueConstraint(
            "retired_by_decision_id",
            name="uq_splitter_cascade_links_retire_decision",
        ),
        CheckConstraint(
            "upstream_output_port_id <> downstream_input_port_id",
            name="ck_splitter_cascade_links_distinct_ports",
        ),
        CheckConstraint(
            "(active AND retired_by_decision_id IS NULL) OR "
            "(NOT active AND retired_by_decision_id IS NOT NULL)",
            name="ck_splitter_cascade_links_retirement",
        ),
        Index(
            "uq_splitter_cascade_links_active_output",
            "upstream_output_port_id",
            unique=True,
            postgresql_where=text("active"),
            sqlite_where=text("active"),
        ),
        Index(
            "uq_splitter_cascade_links_active_input",
            "downstream_input_port_id",
            unique=True,
            postgresql_where=text("active"),
            sqlite_where=text("active"),
        ),
        Index(
            "ix_splitter_cascade_links_downstream_input",
            "downstream_input_port_id",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    upstream_output_port_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("splitter_ports.id", ondelete="RESTRICT"),
        nullable=False,
    )
    downstream_input_port_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("splitter_ports.id", ondelete="RESTRICT"),
        nullable=False,
    )
    created_by_decision_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("fiber_access_attachment_decisions.id", ondelete="RESTRICT"),
        nullable=False,
    )
    retired_by_decision_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("fiber_access_attachment_decisions.id", ondelete="RESTRICT"),
    )
    active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    notes: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(UTC), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(UTC),
        onupdate=lambda: datetime.now(UTC),
        nullable=False,
    )
