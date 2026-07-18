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


class FiberTopologyConnectivityProposalBatch(Base):
    """Immutable operator manifest for explicit staged-path connectivity."""

    __tablename__ = "fiber_topology_connectivity_proposal_batches"
    __table_args__ = (
        UniqueConstraint(
            "manifest_sha256",
            name="uq_fiber_connectivity_proposal_batch_manifest",
        ),
        UniqueConstraint(
            "request_sha256",
            name="uq_fiber_connectivity_proposal_batch_request",
        ),
        CheckConstraint(
            "length(manifest_sha256) = 64 AND length(request_sha256) = 64",
            name="ck_fiber_connectivity_proposal_batch_hashes",
        ),
        CheckConstraint(
            "item_count BETWEEN 1 AND 500",
            name="ck_fiber_connectivity_proposal_batch_item_count",
        ),
        Index("ix_fiber_connectivity_proposal_batch_created", "created_at"),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    manifest_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    request_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    manifest_payload: Mapped[dict] = mapped_column(JSON, nullable=False)
    item_count: Mapped[int] = mapped_column(Integer, nullable=False)
    source_name: Mapped[str] = mapped_column(String(255), nullable=False)
    proposed_by: Mapped[str] = mapped_column(String(160), nullable=False)
    reason: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(UTC), nullable=False
    )

    decisions = relationship(
        "FiberTopologyConnectivityDecision",
        back_populates="proposal_batch",
        order_by="FiberTopologyConnectivityDecision.proposal_batch_row_number",
    )
    batch_review = relationship(
        "FiberTopologyConnectivityBatchReview",
        back_populates="proposal_batch",
        uselist=False,
    )
    runs = relationship(
        "FiberTopologyConnectivityRun",
        back_populates="proposal_batch",
        order_by="FiberTopologyConnectivityRun.executed_at",
    )


class FiberTopologyConnectivityBatchReview(Base):
    """Independent all-or-nothing attestation over one exact manifest."""

    __tablename__ = "fiber_topology_connectivity_batch_reviews"
    __table_args__ = (
        UniqueConstraint(
            "proposal_batch_id", name="uq_fiber_connectivity_batch_review_batch"
        ),
        UniqueConstraint(
            "attestation_sha256",
            name="uq_fiber_connectivity_batch_review_attestation",
        ),
        CheckConstraint(
            "action IN ('approve', 'decline')",
            name="ck_fiber_connectivity_batch_review_action",
        ),
        CheckConstraint(
            "proposed_by <> reviewed_by",
            name="ck_fiber_connectivity_batch_review_separation",
        ),
        CheckConstraint(
            "length(batch_manifest_sha256) = 64 AND length(attestation_sha256) = 64",
            name="ck_fiber_connectivity_batch_review_hashes",
        ),
        CheckConstraint(
            "item_count BETWEEN 1 AND 500",
            name="ck_fiber_connectivity_batch_review_item_count",
        ),
        Index("ix_fiber_connectivity_batch_review_reviewed", "reviewed_at"),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    proposal_batch_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey(
            "fiber_topology_connectivity_proposal_batches.id", ondelete="RESTRICT"
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
        "FiberTopologyConnectivityProposalBatch", back_populates="batch_review"
    )
    runs = relationship(
        "FiberTopologyConnectivityRun",
        back_populates="batch_review",
        order_by="FiberTopologyConnectivityRun.executed_at",
    )


class FiberTopologyConnectivityRun(Base):
    """Immutable evidence for one bounded execute or reconcile pass."""

    __tablename__ = "fiber_topology_connectivity_runs"
    __table_args__ = (
        UniqueConstraint("result_sha256", name="uq_fiber_connectivity_run_result"),
        CheckConstraint(
            "run_type IN ('execute', 'reconcile')",
            name="ck_fiber_connectivity_run_type",
        ),
        CheckConstraint(
            "length(batch_manifest_sha256) = 64 AND length(result_sha256) = 64",
            name="ck_fiber_connectivity_run_hashes",
        ),
        CheckConstraint(
            "requested_limit BETWEEN 1 AND 100",
            name="ck_fiber_connectivity_run_limit",
        ),
        CheckConstraint(
            "scanned_count >= 0 AND endpoint_pending_count >= 0 "
            "AND segment_pending_count >= 0 AND applied_count >= 0 "
            "AND closed_count >= 0 AND error_count >= 0 "
            "AND remaining_actionable_count >= 0",
            name="ck_fiber_connectivity_run_counts",
        ),
        CheckConstraint(
            "scanned_count = endpoint_pending_count + segment_pending_count "
            "+ applied_count + closed_count + error_count",
            name="ck_fiber_connectivity_run_outcomes",
        ),
        Index("ix_fiber_connectivity_run_batch", "proposal_batch_id"),
        Index("ix_fiber_connectivity_run_executed", "executed_at"),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    proposal_batch_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey(
            "fiber_topology_connectivity_proposal_batches.id", ondelete="RESTRICT"
        ),
        nullable=False,
    )
    batch_review_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("fiber_topology_connectivity_batch_reviews.id", ondelete="RESTRICT"),
        nullable=False,
    )
    batch_manifest_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    run_type: Mapped[str] = mapped_column(String(16), nullable=False)
    executed_by: Mapped[str] = mapped_column(String(160), nullable=False)
    requested_limit: Mapped[int] = mapped_column(Integer, nullable=False)
    scanned_count: Mapped[int] = mapped_column(Integer, nullable=False)
    endpoint_pending_count: Mapped[int] = mapped_column(Integer, nullable=False)
    segment_pending_count: Mapped[int] = mapped_column(Integer, nullable=False)
    applied_count: Mapped[int] = mapped_column(Integer, nullable=False)
    closed_count: Mapped[int] = mapped_column(Integer, nullable=False)
    error_count: Mapped[int] = mapped_column(Integer, nullable=False)
    remaining_actionable_count: Mapped[int] = mapped_column(Integer, nullable=False)
    result_payload: Mapped[dict] = mapped_column(JSON, nullable=False)
    result_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    executed_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(UTC), nullable=False
    )

    proposal_batch = relationship(
        "FiberTopologyConnectivityProposalBatch", back_populates="runs"
    )
    batch_review = relationship(
        "FiberTopologyConnectivityBatchReview", back_populates="runs"
    )


__all__ = [
    "FiberTopologyConnectivityBatchReview",
    "FiberTopologyConnectivityProposalBatch",
    "FiberTopologyConnectivityRun",
]
