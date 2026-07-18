from __future__ import annotations

import uuid
from datetime import UTC, datetime

from sqlalchemy import (
    JSON,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    text,
)
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db import Base


class OntAssignmentIdentityDecision(Base):
    """Reviewed repair of one exact ONT-to-subscription electronic identity."""

    __tablename__ = "ont_assignment_identity_decisions"
    __table_args__ = (
        Index(
            "uq_ont_assignment_identity_active_primary",
            "primary_assignment_id",
            unique=True,
            postgresql_where=text("status IN ('proposed', 'approved')"),
            sqlite_where=text("status IN ('proposed', 'approved')"),
        ),
        Index("ix_ont_assignment_identity_status", "status"),
        UniqueConstraint(
            "decision_sha256",
            name="uq_ont_assignment_identity_decision_sha256",
        ),
        CheckConstraint(
            "action IN ('canonicalize', 'deactivate')",
            name="ck_ont_assignment_identity_action",
        ),
        CheckConstraint(
            "status IN ('proposed', 'approved', 'declined', 'applied', 'closed')",
            name="ck_ont_assignment_identity_status",
        ),
        CheckConstraint(
            "(action = 'canonicalize' AND target_subscription_id IS NOT NULL "
            "AND target_subscriber_id IS NOT NULL AND target_pon_port_id IS NOT NULL "
            "AND target_olt_id IS NOT NULL) OR "
            "(action = 'deactivate' AND target_subscription_id IS NULL "
            "AND target_subscriber_id IS NULL AND target_pon_port_id IS NULL "
            "AND target_olt_id IS NULL)",
            name="ck_ont_assignment_identity_targets",
        ),
        CheckConstraint(
            "reviewed_by IS NULL OR reviewed_by <> proposed_by",
            name="ck_ont_assignment_identity_separation",
        ),
        CheckConstraint(
            "(status = 'proposed' AND reviewed_by IS NULL AND reviewed_at IS NULL) OR "
            "(status <> 'proposed' AND reviewed_by IS NOT NULL AND reviewed_at IS NOT NULL)",
            name="ck_ont_assignment_identity_review_evidence",
        ),
        CheckConstraint(
            "(status IN ('applied', 'closed') AND executed_by IS NOT NULL "
            "AND executed_at IS NOT NULL AND result_payload IS NOT NULL "
            "AND result_sha256 IS NOT NULL) OR "
            "(status NOT IN ('applied', 'closed') AND executed_by IS NULL "
            "AND executed_at IS NULL AND result_payload IS NULL "
            "AND result_sha256 IS NULL)",
            name="ck_ont_assignment_identity_result_evidence",
        ),
        CheckConstraint(
            "length(input_sha256) = 64",
            name="ck_ont_assignment_identity_input_sha256",
        ),
        CheckConstraint(
            "length(decision_sha256) = 64",
            name="ck_ont_assignment_identity_decision_sha256",
        ),
        CheckConstraint(
            "result_sha256 IS NULL OR length(result_sha256) = 64",
            name="ck_ont_assignment_identity_result_sha256",
        ),
        CheckConstraint(
            "(proposal_batch_id IS NULL AND proposal_batch_row_number IS NULL) OR "
            "(proposal_batch_id IS NOT NULL AND proposal_batch_row_number > 0)",
            name="ck_ont_assignment_identity_batch_evidence",
        ),
        UniqueConstraint(
            "proposal_batch_id",
            "proposal_batch_row_number",
            name="uq_ont_assignment_identity_batch_row",
        ),
        Index("ix_ont_assignment_identity_batch", "proposal_batch_id"),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    action: Mapped[str] = mapped_column(String(20), nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False)
    primary_assignment_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("ont_assignments.id", ondelete="RESTRICT"),
        nullable=False,
    )
    ont_unit_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("ont_units.id", ondelete="RESTRICT"),
        nullable=False,
    )
    target_subscription_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("subscriptions.id", ondelete="RESTRICT"),
    )
    target_subscriber_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("subscribers.id", ondelete="RESTRICT"),
    )
    target_pon_port_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("pon_ports.id", ondelete="RESTRICT"),
    )
    target_olt_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("olt_devices.id", ondelete="RESTRICT"),
    )
    duplicate_assignment_ids: Mapped[list[str]] = mapped_column(JSON, nullable=False)
    input_snapshot: Mapped[dict[str, object]] = mapped_column(JSON, nullable=False)
    input_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
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
    result_payload: Mapped[dict[str, object] | None] = mapped_column(JSON)
    result_sha256: Mapped[str | None] = mapped_column(String(64))
    proposal_batch_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey(
            "ont_assignment_cutover_proposal_batches.id",
            ondelete="RESTRICT",
        ),
    )
    proposal_batch_row_number: Mapped[int | None] = mapped_column(Integer)

    cutover_proposal_batch = relationship(
        "OntAssignmentCutoverProposalBatch", back_populates="decisions"
    )
