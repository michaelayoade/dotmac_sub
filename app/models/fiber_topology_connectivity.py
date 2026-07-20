from __future__ import annotations

import uuid
from datetime import UTC, datetime

from sqlalchemy import (
    CheckConstraint,
    DateTime,
    Float,
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


class FiberTopologyConnectivityDecision(Base):
    """Source-bound decision for one explicit termination-to-termination edge."""

    __tablename__ = "fiber_topology_connectivity_decisions"
    __table_args__ = (
        Index(
            "uq_fiber_connectivity_active_source",
            "source_system",
            "source_asset_type",
            "source_external_id",
            unique=True,
            postgresql_where=text(
                "status IN ('proposed', 'approved', 'endpoint_change_requested', "
                "'segment_change_requested')"
            ),
            sqlite_where=text(
                "status IN ('proposed', 'approved', 'endpoint_change_requested', "
                "'segment_change_requested')"
            ),
        ),
        UniqueConstraint(
            "decision_sha256", name="uq_fiber_connectivity_decision_sha256"
        ),
        UniqueConstraint(
            "segment_change_request_id",
            name="uq_fiber_connectivity_segment_request",
        ),
        UniqueConstraint(
            "proposal_batch_id",
            "proposal_batch_row_number",
            name="uq_fiber_connectivity_decision_batch_row",
        ),
        CheckConstraint(
            "action IN ('create', 'link_existing', 'reject')",
            name="ck_fiber_connectivity_action",
        ),
        CheckConstraint(
            "status IN ('proposed', 'approved', 'declined', "
            "'endpoint_change_requested', 'segment_change_requested', "
            "'applied', 'closed')",
            name="ck_fiber_connectivity_status",
        ),
        CheckConstraint(
            "(action = 'reject' AND start_endpoint_type IS NULL "
            "AND start_endpoint_ref_id IS NULL AND end_endpoint_type IS NULL "
            "AND end_endpoint_ref_id IS NULL AND target_segment_id IS NULL) OR "
            "(action = 'create' AND start_endpoint_type IS NOT NULL "
            "AND start_endpoint_ref_id IS NOT NULL AND end_endpoint_type IS NOT NULL "
            "AND end_endpoint_ref_id IS NOT NULL AND target_segment_id IS NULL) OR "
            "(action = 'link_existing' AND start_endpoint_type IS NOT NULL "
            "AND start_endpoint_ref_id IS NOT NULL AND end_endpoint_type IS NOT NULL "
            "AND end_endpoint_ref_id IS NOT NULL AND target_segment_id IS NOT NULL)",
            name="ck_fiber_connectivity_action_endpoints",
        ),
        CheckConstraint(
            "start_endpoint_ref_id IS NULL OR end_endpoint_ref_id IS NULL OR "
            "start_endpoint_type <> end_endpoint_type OR "
            "start_endpoint_ref_id <> end_endpoint_ref_id",
            name="ck_fiber_connectivity_distinct_endpoints",
        ),
        CheckConstraint(
            "reviewed_by IS NULL OR reviewed_by <> proposed_by",
            name="ck_fiber_connectivity_separation",
        ),
        CheckConstraint(
            "(status = 'proposed' AND reviewed_by IS NULL AND reviewed_at IS NULL) "
            "OR (status <> 'proposed' AND reviewed_by IS NOT NULL "
            "AND reviewed_at IS NOT NULL)",
            name="ck_fiber_connectivity_review_evidence",
        ),
        CheckConstraint(
            "(status IN ('proposed', 'approved', 'declined') "
            "AND executed_by IS NULL AND executed_at IS NULL) OR "
            "(status NOT IN ('proposed', 'approved', 'declined') "
            "AND executed_by IS NOT NULL AND executed_at IS NOT NULL)",
            name="ck_fiber_connectivity_execution_evidence",
        ),
        CheckConstraint(
            "(status IN ('applied', 'closed') AND finalized_by IS NOT NULL "
            "AND finalized_at IS NOT NULL) OR "
            "(status NOT IN ('applied', 'closed') AND finalized_by IS NULL "
            "AND finalized_at IS NULL)",
            name="ck_fiber_connectivity_finalization_evidence",
        ),
        CheckConstraint(
            "length(feature_content_sha256) = 64",
            name="ck_fiber_connectivity_feature_sha256",
        ),
        CheckConstraint(
            "length(decision_sha256) = 64",
            name="ck_fiber_connectivity_decision_sha256",
        ),
        CheckConstraint(
            "fiber_count IS NULL OR fiber_count > 0",
            name="ck_fiber_connectivity_fiber_count",
        ),
        CheckConstraint(
            "action <> 'create' OR fiber_count IS NOT NULL",
            name="ck_fiber_connectivity_create_capacity",
        ),
        CheckConstraint(
            "length_m IS NULL OR length_m > 0",
            name="ck_fiber_connectivity_length",
        ),
        CheckConstraint(
            "(proposal_batch_id IS NULL AND proposal_batch_row_number IS NULL) OR "
            "(proposal_batch_id IS NOT NULL AND proposal_batch_row_number > 0)",
            name="ck_fiber_connectivity_decision_batch_evidence",
        ),
        Index("ix_fiber_connectivity_status", "status"),
        Index("ix_fiber_connectivity_staged_feature", "staged_feature_id"),
        Index("ix_fiber_connectivity_decision_batch", "proposal_batch_id"),
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
    source_external_id: Mapped[str] = mapped_column(String(255), nullable=False)
    feature_content_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    action: Mapped[str] = mapped_column(String(24), nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False)
    start_endpoint_type: Mapped[str | None] = mapped_column(String(40))
    start_endpoint_ref_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    end_endpoint_type: Mapped[str | None] = mapped_column(String(40))
    end_endpoint_ref_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    segment_type: Mapped[str | None] = mapped_column(String(32))
    cable_type: Mapped[str | None] = mapped_column(String(32))
    fiber_count: Mapped[int | None] = mapped_column(Integer)
    length_m: Mapped[float | None] = mapped_column(Float)
    target_segment_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("fiber_segments.id", ondelete="RESTRICT")
    )
    start_resolution_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("fiber_topology_termination_resolutions.id", ondelete="RESTRICT"),
    )
    end_resolution_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("fiber_topology_termination_resolutions.id", ondelete="RESTRICT"),
    )
    segment_change_request_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("fiber_change_requests.id", ondelete="RESTRICT"),
    )
    canonical_segment_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("fiber_segments.id", ondelete="RESTRICT")
    )
    proposal_batch_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey(
            "fiber_topology_connectivity_proposal_batches.id", ondelete="RESTRICT"
        ),
    )
    proposal_batch_row_number: Mapped[int | None] = mapped_column(Integer)
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

    staged_feature = relationship("FiberTopologyStagedFeature")
    target_segment = relationship("FiberSegment", foreign_keys=[target_segment_id])
    canonical_segment = relationship(
        "FiberSegment", foreign_keys=[canonical_segment_id]
    )
    start_resolution = relationship(
        "FiberTopologyTerminationResolution",
        foreign_keys=[start_resolution_id],
    )
    end_resolution = relationship(
        "FiberTopologyTerminationResolution",
        foreign_keys=[end_resolution_id],
    )
    segment_change_request = relationship("FiberChangeRequest")
    proposal_batch = relationship(
        "FiberTopologyConnectivityProposalBatch", back_populates="decisions"
    )
    source_link = relationship(
        "FiberTopologySegmentSourceLink", back_populates="decision", uselist=False
    )


class FiberTopologyTerminationResolution(Base):
    """Single canonical resolution for one typed network endpoint reference."""

    __tablename__ = "fiber_topology_termination_resolutions"
    __table_args__ = (
        UniqueConstraint(
            "endpoint_type",
            "endpoint_ref_id",
            name="uq_fiber_termination_resolution_endpoint",
        ),
        UniqueConstraint(
            "change_request_id", name="uq_fiber_termination_resolution_request"
        ),
        UniqueConstraint(
            "termination_point_id", name="uq_fiber_termination_resolution_point"
        ),
        CheckConstraint(
            "status IN ('pending', 'applied', 'rejected')",
            name="ck_fiber_termination_resolution_status",
        ),
        CheckConstraint(
            "(status = 'pending' AND change_request_id IS NOT NULL "
            "AND termination_point_id IS NULL) OR "
            "(status = 'applied' AND termination_point_id IS NOT NULL) OR "
            "(status = 'rejected' AND change_request_id IS NOT NULL "
            "AND termination_point_id IS NULL)",
            name="ck_fiber_termination_resolution_evidence",
        ),
        Index("ix_fiber_termination_resolution_status", "status"),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    endpoint_type: Mapped[str] = mapped_column(String(40), nullable=False)
    endpoint_ref_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), nullable=False
    )
    status: Mapped[str] = mapped_column(String(20), nullable=False)
    source_decision_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("fiber_topology_connectivity_decisions.id", ondelete="RESTRICT"),
        nullable=False,
    )
    change_request_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("fiber_change_requests.id", ondelete="RESTRICT"),
    )
    termination_point_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("fiber_termination_points.id", ondelete="RESTRICT"),
    )
    requested_by: Mapped[str] = mapped_column(String(160), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(UTC), nullable=False
    )
    resolved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    source_decision = relationship(
        "FiberTopologyConnectivityDecision", foreign_keys=[source_decision_id]
    )
    change_request = relationship("FiberChangeRequest")
    termination_point = relationship("FiberTerminationPoint")


class FiberTopologySegmentSourceLink(Base):
    """Canonical segment provenance projected from an applied path decision."""

    __tablename__ = "fiber_topology_segment_source_links"
    __table_args__ = (
        UniqueConstraint(
            "source_system",
            "source_asset_type",
            "external_id",
            name="uq_fiber_segment_source_link_identity",
        ),
        UniqueConstraint("decision_id", name="uq_fiber_segment_source_link_decision"),
        CheckConstraint(
            "status IN ('active', 'retired')",
            name="ck_fiber_segment_source_link_status",
        ),
        CheckConstraint(
            "length(content_sha256) = 64",
            name="ck_fiber_segment_source_link_sha256",
        ),
        Index("ix_fiber_segment_source_link_segment", "segment_id"),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    decision_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("fiber_topology_connectivity_decisions.id", ondelete="RESTRICT"),
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
    segment_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("fiber_segments.id", ondelete="RESTRICT"),
        nullable=False,
    )
    status: Mapped[str] = mapped_column(String(20), nullable=False, default="active")
    linked_by: Mapped[str] = mapped_column(String(160), nullable=False)
    linked_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(UTC), nullable=False
    )

    decision = relationship(
        "FiberTopologyConnectivityDecision", back_populates="source_link"
    )
    staged_feature = relationship("FiberTopologyStagedFeature")
    segment = relationship("FiberSegment")


__all__ = [
    "FiberTopologyConnectivityDecision",
    "FiberTopologySegmentSourceLink",
    "FiberTopologyTerminationResolution",
]
