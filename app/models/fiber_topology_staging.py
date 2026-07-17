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
    UniqueConstraint,
)
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db import Base


class FiberTopologySourceBatch(Base):
    """Immutable manifest for one staged topology source file."""

    __tablename__ = "fiber_topology_source_batches"
    __table_args__ = (
        UniqueConstraint(
            "source_system",
            "profile",
            "manifest_sha256",
            name="uq_fiber_topology_batch_source_profile_manifest",
        ),
        CheckConstraint(
            "status IN ('staged', 'blocked')",
            name="ck_fiber_topology_batch_status",
        ),
        CheckConstraint(
            "(status = 'blocked' AND blocker_count > 0) OR "
            "(status = 'staged' AND blocker_count = 0)",
            name="ck_fiber_topology_batch_status_blockers",
        ),
        CheckConstraint(
            "feature_count >= 0 AND blocker_count >= 0 AND candidate_count >= 0 "
            "AND unchanged_count >= 0 AND new_count >= 0 AND "
            "blocker_count + candidate_count + unchanged_count + new_count "
            "= feature_count",
            name="ck_fiber_topology_batch_status_counts",
        ),
        CheckConstraint(
            "length(file_sha256) = 64",
            name="ck_fiber_topology_batch_file_sha256",
        ),
        CheckConstraint(
            "length(manifest_sha256) = 64",
            name="ck_fiber_topology_batch_manifest_sha256",
        ),
        Index(
            "ix_fiber_topology_batch_profile_created",
            "profile",
            "created_at",
        ),
        Index(
            "ix_fiber_topology_batch_file_sha256",
            "file_sha256",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    source_system: Mapped[str] = mapped_column(String(40), nullable=False)
    profile: Mapped[str] = mapped_column(String(40), nullable=False)
    source_name: Mapped[str] = mapped_column(String(255), nullable=False)
    asset_type: Mapped[str] = mapped_column(String(40), nullable=False)
    external_id_key: Mapped[str] = mapped_column(String(80), nullable=False)
    file_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    manifest_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    status: Mapped[str] = mapped_column(String(20), nullable=False)
    feature_count: Mapped[int] = mapped_column(Integer, nullable=False)
    blocker_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    candidate_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    unchanged_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    new_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    source_metadata: Mapped[dict | None] = mapped_column(JSON)
    created_by: Mapped[str] = mapped_column(String(160), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(UTC), nullable=False
    )

    features = relationship(
        "FiberTopologyStagedFeature",
        back_populates="batch",
        cascade="all, delete-orphan",
        order_by="FiberTopologyStagedFeature.row_number",
    )


class FiberTopologyStagedFeature(Base):
    """Normalized source fact and non-authoritative canonical match suggestion."""

    __tablename__ = "fiber_topology_staged_features"
    __table_args__ = (
        UniqueConstraint(
            "batch_id",
            "row_number",
            name="uq_fiber_topology_staged_feature_batch_row",
        ),
        CheckConstraint(
            "match_status IN "
            "('new', 'unchanged', 'exact_external', 'candidate', "
            "'ambiguous', 'blocked')",
            name="ck_fiber_topology_staged_feature_match_status",
        ),
        CheckConstraint(
            "external_id IS NOT NULL OR match_status = 'blocked'",
            name="ck_fiber_topology_staged_feature_identity",
        ),
        CheckConstraint(
            "length(content_sha256) = 64",
            name="ck_fiber_topology_staged_feature_content_sha256",
        ),
        CheckConstraint(
            "length(geometry_sha256) = 64",
            name="ck_fiber_topology_staged_feature_geometry_sha256",
        ),
        Index(
            "ix_fiber_topology_staged_feature_identity",
            "asset_type",
            "external_id",
        ),
        Index(
            "ix_fiber_topology_staged_feature_content_sha256",
            "content_sha256",
        ),
        Index(
            "ix_fiber_topology_staged_feature_geometry_sha256",
            "geometry_sha256",
        ),
        Index(
            "ix_fiber_topology_staged_feature_match_status",
            "match_status",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    batch_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("fiber_topology_source_batches.id", ondelete="CASCADE"),
        nullable=False,
    )
    row_number: Mapped[int] = mapped_column(Integer, nullable=False)
    asset_type: Mapped[str] = mapped_column(String(40), nullable=False)
    external_id: Mapped[str | None] = mapped_column(String(255))
    display_name: Mapped[str | None] = mapped_column(String(255))
    geometry_type: Mapped[str] = mapped_column(String(20), nullable=False)
    geometry_geojson: Mapped[dict] = mapped_column(JSON, nullable=False)
    source_properties: Mapped[dict] = mapped_column(JSON, nullable=False)
    content_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    geometry_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    match_status: Mapped[str] = mapped_column(String(20), nullable=False)
    blocker_codes: Mapped[list] = mapped_column(JSON, nullable=False, default=list)
    match_reasons: Mapped[list] = mapped_column(JSON, nullable=False, default=list)
    candidate_asset_ids: Mapped[list] = mapped_column(
        JSON, nullable=False, default=list
    )
    canonical_asset_type: Mapped[str | None] = mapped_column(String(40))
    canonical_asset_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    prior_feature_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("fiber_topology_staged_features.id", ondelete="SET NULL"),
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(UTC), nullable=False
    )

    batch = relationship("FiberTopologySourceBatch", back_populates="features")
    prior_feature = relationship(
        "FiberTopologyStagedFeature",
        remote_side="FiberTopologyStagedFeature.id",
        foreign_keys=[prior_feature_id],
    )


__all__ = ["FiberTopologySourceBatch", "FiberTopologyStagedFeature"]
