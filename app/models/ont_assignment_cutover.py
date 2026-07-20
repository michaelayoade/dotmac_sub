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
)
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db import Base


class OntAssignmentCutoverProposalBatch(Base):
    """Immutable exact manifest of operator-selected assignment repairs."""

    __tablename__ = "ont_assignment_cutover_proposal_batches"
    __table_args__ = (
        UniqueConstraint(
            "request_sha256",
            name="uq_ont_assignment_cutover_batch_request",
        ),
        UniqueConstraint(
            "manifest_sha256",
            name="uq_ont_assignment_cutover_batch_manifest",
        ),
        CheckConstraint(
            "length(report_sha256) = 64",
            name="ck_ont_assignment_cutover_batch_report_sha256",
        ),
        CheckConstraint(
            "length(request_sha256) = 64",
            name="ck_ont_assignment_cutover_batch_request_sha256",
        ),
        CheckConstraint(
            "length(manifest_sha256) = 64",
            name="ck_ont_assignment_cutover_batch_manifest_sha256",
        ),
        CheckConstraint(
            "item_count BETWEEN 1 AND 100",
            name="ck_ont_assignment_cutover_batch_item_count",
        ),
        Index("ix_ont_assignment_cutover_batch_created", "created_at"),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    report_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    request_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    manifest_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    manifest_payload: Mapped[dict[str, object]] = mapped_column(JSON, nullable=False)
    item_count: Mapped[int] = mapped_column(Integer, nullable=False)
    source_name: Mapped[str] = mapped_column(String(255), nullable=False)
    proposed_by: Mapped[str] = mapped_column(String(160), nullable=False)
    reason: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(UTC), nullable=False
    )

    decisions = relationship(
        "OntAssignmentIdentityDecision",
        back_populates="cutover_proposal_batch",
        order_by="OntAssignmentIdentityDecision.proposal_batch_row_number",
    )
    batch_review = relationship(
        "OntAssignmentCutoverBatchReview",
        back_populates="proposal_batch",
        uselist=False,
    )
    verification_attestations = relationship(
        "OntAssignmentCutoverVerificationAttestation",
        back_populates="proposal_batch",
        order_by="OntAssignmentCutoverVerificationAttestation.verified_at",
    )


class OntAssignmentCutoverBatchReview(Base):
    """Independent approve/decline attestation over one immutable manifest."""

    __tablename__ = "ont_assignment_cutover_batch_reviews"
    __table_args__ = (
        UniqueConstraint(
            "proposal_batch_id",
            name="uq_ont_assignment_cutover_batch_review_batch",
        ),
        UniqueConstraint(
            "attestation_sha256",
            name="uq_ont_assignment_cutover_batch_review_attestation",
        ),
        CheckConstraint(
            "action IN ('approve', 'decline')",
            name="ck_ont_assignment_cutover_batch_review_action",
        ),
        CheckConstraint(
            "proposed_by <> reviewed_by",
            name="ck_ont_assignment_cutover_batch_review_separation",
        ),
        CheckConstraint(
            "length(batch_manifest_sha256) = 64",
            name="ck_ont_assignment_cutover_batch_review_manifest_sha256",
        ),
        CheckConstraint(
            "length(attestation_sha256) = 64",
            name="ck_ont_assignment_cutover_batch_review_sha256",
        ),
        CheckConstraint(
            "item_count BETWEEN 1 AND 100",
            name="ck_ont_assignment_cutover_batch_review_item_count",
        ),
        Index("ix_ont_assignment_cutover_batch_reviewed", "reviewed_at"),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    proposal_batch_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey(
            "ont_assignment_cutover_proposal_batches.id",
            ondelete="RESTRICT",
        ),
        nullable=False,
    )
    batch_manifest_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    action: Mapped[str] = mapped_column(String(16), nullable=False)
    proposed_by: Mapped[str] = mapped_column(String(160), nullable=False)
    reviewed_by: Mapped[str] = mapped_column(String(160), nullable=False)
    review_notes: Mapped[str] = mapped_column(Text, nullable=False)
    item_count: Mapped[int] = mapped_column(Integer, nullable=False)
    attestation_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    reviewed_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(UTC), nullable=False
    )

    proposal_batch = relationship(
        "OntAssignmentCutoverProposalBatch", back_populates="batch_review"
    )
    verification_attestations = relationship(
        "OntAssignmentCutoverVerificationAttestation",
        back_populates="batch_review",
        order_by="OntAssignmentCutoverVerificationAttestation.verified_at",
    )


class OntAssignmentCutoverVerificationAttestation(Base):
    """Immutable post-execution evidence over one reviewed cleanup batch."""

    __tablename__ = "ont_assignment_cutover_verification_attestations"
    __table_args__ = (
        UniqueConstraint(
            "proposal_batch_id",
            "evidence_sha256",
            name="uq_ont_assignment_cutover_verification_evidence",
        ),
        UniqueConstraint(
            "attestation_sha256",
            name="uq_ont_assignment_cutover_verification_attestation",
        ),
        CheckConstraint(
            "outcome IN ('declined', 'applied_clean_scope', "
            "'applied_with_residual_findings', "
            "'completed_with_stale_closures', "
            "'completed_with_conflict_closures', "
            "'completed_with_other_closures')",
            name="ck_ont_assignment_cutover_verification_outcome",
        ),
        CheckConstraint(
            "item_count BETWEEN 1 AND 100",
            name="ck_ont_assignment_cutover_verification_item_count",
        ),
        CheckConstraint(
            "pending_count >= 0 AND applied_count >= 0 "
            "AND declined_count >= 0 AND stale_closed_count >= 0 "
            "AND conflict_closed_count >= 0 AND other_closed_count >= 0 "
            "AND batch_scope_residual_count >= 0 "
            "AND global_blocker_assignment_count >= 0",
            name="ck_ont_assignment_cutover_verification_counts_nonnegative",
        ),
        CheckConstraint(
            "item_count = pending_count + applied_count + declined_count "
            "+ stale_closed_count + conflict_closed_count + other_closed_count",
            name="ck_ont_assignment_cutover_verification_counts_total",
        ),
        CheckConstraint(
            "pending_count = 0",
            name="ck_ont_assignment_cutover_verification_terminal",
        ),
        CheckConstraint(
            "length(batch_manifest_sha256) = 64 "
            "AND length(decision_evidence_sha256) = 64 "
            "AND length(fresh_report_sha256) = 64 "
            "AND length(evidence_sha256) = 64 "
            "AND length(attestation_sha256) = 64",
            name="ck_ont_assignment_cutover_verification_hashes",
        ),
        Index(
            "ix_ont_assignment_cutover_verification_batch",
            "proposal_batch_id",
            "verified_at",
        ),
        Index(
            "ix_ont_assignment_cutover_verification_outcome",
            "outcome",
            "verified_at",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    proposal_batch_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey(
            "ont_assignment_cutover_proposal_batches.id",
            ondelete="RESTRICT",
        ),
        nullable=False,
    )
    batch_review_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("ont_assignment_cutover_batch_reviews.id", ondelete="RESTRICT"),
        nullable=False,
    )
    batch_manifest_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    decision_evidence_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    fresh_report_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    evidence_payload: Mapped[dict[str, object]] = mapped_column(JSON, nullable=False)
    evidence_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    outcome: Mapped[str] = mapped_column(String(48), nullable=False)
    item_count: Mapped[int] = mapped_column(Integer, nullable=False)
    pending_count: Mapped[int] = mapped_column(Integer, nullable=False)
    applied_count: Mapped[int] = mapped_column(Integer, nullable=False)
    declined_count: Mapped[int] = mapped_column(Integer, nullable=False)
    stale_closed_count: Mapped[int] = mapped_column(Integer, nullable=False)
    conflict_closed_count: Mapped[int] = mapped_column(Integer, nullable=False)
    other_closed_count: Mapped[int] = mapped_column(Integer, nullable=False)
    batch_scope_residual_count: Mapped[int] = mapped_column(Integer, nullable=False)
    global_blocker_assignment_count: Mapped[int] = mapped_column(
        Integer, nullable=False
    )
    global_cutover_ready: Mapped[bool] = mapped_column(nullable=False)
    verified_by: Mapped[str] = mapped_column(String(160), nullable=False)
    verification_notes: Mapped[str] = mapped_column(Text, nullable=False)
    attestation_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    verified_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(UTC), nullable=False
    )

    proposal_batch = relationship(
        "OntAssignmentCutoverProposalBatch",
        back_populates="verification_attestations",
    )
    batch_review = relationship(
        "OntAssignmentCutoverBatchReview",
        back_populates="verification_attestations",
    )


__all__ = [
    "OntAssignmentCutoverBatchReview",
    "OntAssignmentCutoverProposalBatch",
    "OntAssignmentCutoverVerificationAttestation",
]
