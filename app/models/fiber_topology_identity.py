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


class FiberTopologyIdentityProposalBatch(Base):
    """Immutable operator-scale manifest for identity proposals."""

    __tablename__ = "fiber_topology_identity_proposal_batches"
    __table_args__ = (
        UniqueConstraint(
            "manifest_sha256",
            name="uq_fiber_topology_identity_proposal_batch_manifest",
        ),
        UniqueConstraint(
            "request_sha256",
            name="uq_fiber_topology_identity_proposal_batch_request",
        ),
        CheckConstraint(
            "length(manifest_sha256) = 64",
            name="ck_fiber_topology_identity_proposal_batch_sha256",
        ),
        CheckConstraint(
            "length(request_sha256) = 64",
            name="ck_fiber_topology_identity_proposal_batch_request_sha256",
        ),
        CheckConstraint(
            "item_count > 0",
            name="ck_fiber_topology_identity_proposal_batch_item_count",
        ),
        Index(
            "ix_fiber_topology_identity_proposal_batch_created",
            "created_at",
        ),
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
        "FiberTopologyIdentityDecision",
        back_populates="proposal_batch",
        order_by="FiberTopologyIdentityDecision.proposal_batch_row_number",
    )
    batch_review = relationship(
        "FiberTopologyIdentityBatchReview",
        back_populates="proposal_batch",
        uselist=False,
    )
    execution_runs = relationship(
        "FiberTopologyIdentityExecutionRun",
        back_populates="proposal_batch",
        order_by="FiberTopologyIdentityExecutionRun.executed_at",
    )


class FiberTopologyIdentityBatchReview(Base):
    """Independent attestation over one exact proposal-batch manifest."""

    __tablename__ = "fiber_topology_identity_batch_reviews"
    __table_args__ = (
        UniqueConstraint(
            "proposal_batch_id",
            name="uq_fiber_identity_batch_review_batch",
        ),
        UniqueConstraint(
            "attestation_sha256",
            name="uq_fiber_identity_batch_review_attestation",
        ),
        CheckConstraint(
            "action IN ('approve', 'decline')",
            name="ck_fiber_identity_batch_review_action",
        ),
        CheckConstraint(
            "proposed_by <> reviewed_by",
            name="ck_fiber_identity_batch_review_separation",
        ),
        CheckConstraint(
            "length(batch_manifest_sha256) = 64",
            name="ck_fiber_identity_batch_review_manifest_sha256",
        ),
        CheckConstraint(
            "length(attestation_sha256) = 64",
            name="ck_fiber_identity_batch_review_sha256",
        ),
        CheckConstraint(
            "item_count > 0",
            name="ck_fiber_identity_batch_review_item_count",
        ),
        Index("ix_fiber_identity_batch_review_reviewed", "reviewed_at"),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    proposal_batch_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey(
            "fiber_topology_identity_proposal_batches.id",
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
        "FiberTopologyIdentityProposalBatch", back_populates="batch_review"
    )
    execution_runs = relationship(
        "FiberTopologyIdentityExecutionRun",
        back_populates="batch_review",
        order_by="FiberTopologyIdentityExecutionRun.executed_at",
    )


class FiberTopologyIdentityExecutionRun(Base):
    """Auditable bounded execution of independently approved decisions."""

    __tablename__ = "fiber_topology_identity_execution_runs"
    __table_args__ = (
        UniqueConstraint(
            "result_sha256",
            name="uq_fiber_identity_execution_run_result",
        ),
        CheckConstraint(
            "length(batch_manifest_sha256) = 64",
            name="ck_fiber_identity_execution_manifest_sha256",
        ),
        CheckConstraint(
            "length(result_sha256) = 64",
            name="ck_fiber_identity_execution_result_sha256",
        ),
        CheckConstraint(
            "requested_limit BETWEEN 1 AND 100",
            name="ck_fiber_identity_execution_limit",
        ),
        CheckConstraint(
            "scanned_count >= 0 AND change_requested_count >= 0 "
            "AND applied_count >= 0 AND closed_count >= 0 "
            "AND error_count >= 0 AND remaining_approved_count >= 0",
            name="ck_fiber_identity_execution_counts",
        ),
        CheckConstraint(
            "scanned_count = change_requested_count + applied_count "
            "+ closed_count + error_count",
            name="ck_fiber_identity_execution_outcomes",
        ),
        Index("ix_fiber_identity_execution_batch", "proposal_batch_id"),
        Index("ix_fiber_identity_execution_executed", "executed_at"),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    proposal_batch_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey(
            "fiber_topology_identity_proposal_batches.id",
            ondelete="RESTRICT",
        ),
        nullable=False,
    )
    batch_review_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey(
            "fiber_topology_identity_batch_reviews.id",
            ondelete="RESTRICT",
        ),
        nullable=False,
    )
    batch_manifest_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    executed_by: Mapped[str] = mapped_column(String(160), nullable=False)
    requested_limit: Mapped[int] = mapped_column(Integer, nullable=False)
    scanned_count: Mapped[int] = mapped_column(Integer, nullable=False)
    change_requested_count: Mapped[int] = mapped_column(Integer, nullable=False)
    applied_count: Mapped[int] = mapped_column(Integer, nullable=False)
    closed_count: Mapped[int] = mapped_column(Integer, nullable=False)
    error_count: Mapped[int] = mapped_column(Integer, nullable=False)
    remaining_approved_count: Mapped[int] = mapped_column(Integer, nullable=False)
    result_payload: Mapped[dict] = mapped_column(JSON, nullable=False)
    result_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    executed_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(UTC), nullable=False
    )

    proposal_batch = relationship(
        "FiberTopologyIdentityProposalBatch", back_populates="execution_runs"
    )
    batch_review = relationship(
        "FiberTopologyIdentityBatchReview", back_populates="execution_runs"
    )


class FiberTopologyIdentityDecision(Base):
    """Immutable-source-bound, dual-reviewed identity decision."""

    __tablename__ = "fiber_topology_identity_decisions"
    __table_args__ = (
        Index(
            "uq_fiber_topology_identity_decision_active_feature",
            "staged_feature_id",
            unique=True,
            postgresql_where=text(
                "status IN ('proposed', 'approved', 'change_requested')"
            ),
            sqlite_where=text("status IN ('proposed', 'approved', 'change_requested')"),
        ),
        Index(
            "uq_fiber_topology_identity_decision_active_source",
            "source_system",
            "source_asset_type",
            "source_external_id",
            unique=True,
            postgresql_where=text(
                "source_external_id IS NOT NULL AND "
                "status IN ('proposed', 'approved', 'change_requested')"
            ),
            sqlite_where=text(
                "source_external_id IS NOT NULL AND "
                "status IN ('proposed', 'approved', 'change_requested')"
            ),
        ),
        UniqueConstraint(
            "decision_sha256",
            name="uq_fiber_topology_identity_decision_sha256",
        ),
        UniqueConstraint(
            "change_request_id",
            name="uq_fiber_topology_identity_decision_change_request",
        ),
        UniqueConstraint(
            "proposal_batch_id",
            "proposal_batch_row_number",
            name="uq_fiber_topology_identity_decision_batch_row",
        ),
        CheckConstraint(
            "action IN ('create', 'link_existing', 'reject')",
            name="ck_fiber_topology_identity_decision_action",
        ),
        CheckConstraint(
            "status IN "
            "('proposed', 'approved', 'declined', 'change_requested', "
            "'applied', 'closed')",
            name="ck_fiber_topology_identity_decision_status",
        ),
        CheckConstraint(
            "(action = 'link_existing' AND target_asset_type IS NOT NULL "
            "AND target_asset_id IS NOT NULL) OR "
            "(action <> 'link_existing' AND target_asset_type IS NULL "
            "AND target_asset_id IS NULL)",
            name="ck_fiber_topology_identity_decision_target",
        ),
        CheckConstraint(
            "reviewed_by IS NULL OR reviewed_by <> proposed_by",
            name="ck_fiber_topology_identity_decision_separation",
        ),
        CheckConstraint(
            "(status = 'proposed' AND reviewed_by IS NULL AND reviewed_at IS NULL) "
            "OR (status <> 'proposed' AND reviewed_by IS NOT NULL "
            "AND reviewed_at IS NOT NULL)",
            name="ck_fiber_topology_identity_decision_review_evidence",
        ),
        CheckConstraint(
            "(status IN ('proposed', 'approved', 'declined') "
            "AND executed_by IS NULL "
            "AND executed_at IS NULL) OR "
            "(status NOT IN ('proposed', 'approved', 'declined') "
            "AND executed_by IS NOT NULL "
            "AND executed_at IS NOT NULL)",
            name="ck_fiber_topology_identity_decision_execution_evidence",
        ),
        CheckConstraint(
            "(status IN ('applied', 'closed') AND finalized_by IS NOT NULL "
            "AND finalized_at IS NOT NULL) OR "
            "(status NOT IN ('applied', 'closed') AND finalized_by IS NULL "
            "AND finalized_at IS NULL)",
            name="ck_fiber_topology_identity_decision_finalization_evidence",
        ),
        CheckConstraint(
            "(action = 'create' AND "
            "((status IN ('proposed', 'approved', 'declined') "
            "AND change_request_id IS NULL) "
            "OR (status IN ('change_requested', 'applied', 'closed') "
            "AND change_request_id IS NOT NULL))) OR "
            "(action <> 'create' AND change_request_id IS NULL "
            "AND status <> 'change_requested')",
            name="ck_fiber_topology_identity_decision_change_request",
        ),
        CheckConstraint(
            "(action = 'create') OR "
            "(action = 'link_existing' AND "
            "status IN ('proposed', 'approved', 'declined', 'applied')) OR "
            "(action = 'reject' AND "
            "status IN ('proposed', 'approved', 'declined', 'closed'))",
            name="ck_fiber_topology_identity_decision_action_status",
        ),
        CheckConstraint(
            "length(feature_content_sha256) = 64",
            name="ck_fiber_topology_identity_decision_feature_sha256",
        ),
        CheckConstraint(
            "length(decision_sha256) = 64",
            name="ck_fiber_topology_identity_decision_sha256",
        ),
        CheckConstraint(
            "(proposal_batch_id IS NULL AND proposal_batch_row_number IS NULL) OR "
            "(proposal_batch_id IS NOT NULL AND proposal_batch_row_number > 0)",
            name="ck_fiber_topology_identity_decision_batch_evidence",
        ),
        Index("ix_fiber_topology_identity_decision_status", "status"),
        Index("ix_fiber_topology_identity_decision_action", "action"),
        Index("ix_fiber_topology_identity_decision_batch", "proposal_batch_id"),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    staged_feature_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("fiber_topology_staged_features.id", ondelete="RESTRICT"),
        nullable=False,
    )
    source_system: Mapped[str] = mapped_column(String(40), nullable=False)
    source_asset_type: Mapped[str] = mapped_column(String(40), nullable=False)
    source_external_id: Mapped[str | None] = mapped_column(String(255))
    feature_content_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    action: Mapped[str] = mapped_column(String(24), nullable=False)
    status: Mapped[str] = mapped_column(String(24), nullable=False)
    target_asset_type: Mapped[str | None] = mapped_column(String(40))
    target_asset_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
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
    finalized_by: Mapped[str | None] = mapped_column(String(160))
    finalized_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    closed_reason: Mapped[str | None] = mapped_column(String(160))
    change_request_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("fiber_change_requests.id", ondelete="RESTRICT"),
    )
    proposal_batch_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey(
            "fiber_topology_identity_proposal_batches.id",
            ondelete="RESTRICT",
        ),
    )
    proposal_batch_row_number: Mapped[int | None] = mapped_column(Integer)

    staged_feature = relationship("FiberTopologyStagedFeature")
    change_request = relationship("FiberChangeRequest")
    proposal_batch = relationship(
        "FiberTopologyIdentityProposalBatch", back_populates="decisions"
    )
    source_link = relationship(
        "FiberTopologyAssetSourceLink",
        back_populates="decision",
        uselist=False,
    )


class FiberTopologyAssetSourceLink(Base):
    """Canonical source identity projected from one applied decision."""

    __tablename__ = "fiber_topology_asset_source_links"
    __table_args__ = (
        UniqueConstraint(
            "source_system",
            "source_asset_type",
            "external_id",
            name="uq_fiber_topology_source_link_identity",
        ),
        UniqueConstraint(
            "decision_id",
            name="uq_fiber_topology_source_link_decision",
        ),
        CheckConstraint(
            "status IN ('active', 'retired')",
            name="ck_fiber_topology_source_link_status",
        ),
        CheckConstraint(
            "length(content_sha256) = 64",
            name="ck_fiber_topology_source_link_content_sha256",
        ),
        Index(
            "ix_fiber_topology_source_link_canonical",
            "canonical_asset_type",
            "canonical_asset_id",
        ),
        Index("ix_fiber_topology_source_link_status", "status"),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    decision_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("fiber_topology_identity_decisions.id", ondelete="RESTRICT"),
        nullable=False,
    )
    staged_feature_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("fiber_topology_staged_features.id", ondelete="RESTRICT"),
        nullable=False,
    )
    source_system: Mapped[str] = mapped_column(String(40), nullable=False)
    source_profile: Mapped[str] = mapped_column(String(40), nullable=False)
    source_asset_type: Mapped[str] = mapped_column(String(40), nullable=False)
    external_id: Mapped[str] = mapped_column(String(255), nullable=False)
    content_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    canonical_asset_type: Mapped[str] = mapped_column(String(40), nullable=False)
    canonical_asset_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), nullable=False
    )
    status: Mapped[str] = mapped_column(String(20), nullable=False, default="active")
    linked_by: Mapped[str] = mapped_column(String(160), nullable=False)
    linked_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(UTC), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(UTC),
        onupdate=lambda: datetime.now(UTC),
        nullable=False,
    )

    decision = relationship(
        "FiberTopologyIdentityDecision", back_populates="source_link"
    )
    staged_feature = relationship("FiberTopologyStagedFeature")


__all__ = [
    "FiberTopologyAssetSourceLink",
    "FiberTopologyIdentityBatchReview",
    "FiberTopologyIdentityDecision",
    "FiberTopologyIdentityExecutionRun",
    "FiberTopologyIdentityProposalBatch",
]
