from __future__ import annotations

import uuid
from datetime import UTC, datetime

from sqlalchemy import (
    JSON,
    CheckConstraint,
    DateTime,
    Float,
    ForeignKey,
    Index,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db import Base


class FiberTopologyFieldObservation(Base):
    """Immutable technician evidence bound to one staged source version."""

    __tablename__ = "fiber_topology_field_observations"
    __table_args__ = (
        UniqueConstraint(
            "observation_sha256",
            name="uq_fiber_topology_field_observation_sha256",
        ),
        UniqueConstraint(
            "client_ref",
            name="uq_fiber_topology_field_observation_client_ref",
        ),
        CheckConstraint(
            "verification_scope IN "
            "('identity', 'presence', 'start_endpoint', 'end_endpoint', "
            "'path_endpoints')",
            name="ck_fiber_topology_field_observation_scope",
        ),
        CheckConstraint(
            "outcome IN "
            "('agrees', 'conflicts', 'not_found', 'inaccessible', 'inconclusive')",
            name="ck_fiber_topology_field_observation_outcome",
        ),
        CheckConstraint(
            "length(feature_content_sha256) = 64 "
            "AND length(claim_sha256) = 64 "
            "AND length(observation_sha256) = 64",
            name="ck_fiber_topology_field_observation_hashes",
        ),
        CheckConstraint(
            "(observed_asset_type IS NULL AND observed_asset_id IS NULL) OR "
            "(observed_asset_type IS NOT NULL AND observed_asset_id IS NOT NULL)",
            name="ck_fiber_topology_field_observation_asset_pair",
        ),
        CheckConstraint(
            "(start_endpoint_type IS NULL AND start_endpoint_ref_id IS NULL) OR "
            "(start_endpoint_type IS NOT NULL AND start_endpoint_ref_id IS NOT NULL)",
            name="ck_fiber_topology_field_observation_start_pair",
        ),
        CheckConstraint(
            "(end_endpoint_type IS NULL AND end_endpoint_ref_id IS NULL) OR "
            "(end_endpoint_type IS NOT NULL AND end_endpoint_ref_id IS NOT NULL)",
            name="ck_fiber_topology_field_observation_end_pair",
        ),
        CheckConstraint(
            "(latitude IS NULL AND longitude IS NULL) OR "
            "(latitude BETWEEN -90 AND 90 AND longitude BETWEEN -180 AND 180)",
            name="ck_fiber_topology_field_observation_coordinates",
        ),
        CheckConstraint(
            "accuracy_m IS NULL OR "
            "(latitude IS NOT NULL AND longitude IS NOT NULL "
            "AND accuracy_m BETWEEN 0 AND 10000)",
            name="ck_fiber_topology_field_observation_accuracy",
        ),
        Index(
            "ix_fiber_topology_field_observation_source",
            "source_system",
            "source_asset_type",
            "source_external_id",
        ),
        Index(
            "ix_fiber_topology_field_observation_feature_content",
            "staged_feature_id",
            "feature_content_sha256",
        ),
        Index(
            "ix_fiber_topology_field_observation_work_order",
            "work_order_id",
            "observed_at",
        ),
        Index(
            "ix_fiber_topology_field_observation_observed",
            "observed_at",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    staged_feature_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("fiber_topology_staged_features.id", ondelete="RESTRICT"),
        nullable=False,
    )
    feature_content_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    source_system: Mapped[str] = mapped_column(String(40), nullable=False)
    source_profile: Mapped[str] = mapped_column(String(40), nullable=False)
    source_asset_type: Mapped[str] = mapped_column(String(40), nullable=False)
    source_external_id: Mapped[str | None] = mapped_column(String(255))

    work_order_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("work_order.id", ondelete="RESTRICT"),
        nullable=False,
    )
    work_order_public_id: Mapped[str] = mapped_column(String(64), nullable=False)
    verification_scope: Mapped[str] = mapped_column(String(32), nullable=False)
    outcome: Mapped[str] = mapped_column(String(24), nullable=False)
    observed_external_label: Mapped[str | None] = mapped_column(String(255))
    observed_asset_type: Mapped[str | None] = mapped_column(String(40))
    observed_asset_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    start_endpoint_type: Mapped[str | None] = mapped_column(String(40))
    start_endpoint_ref_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    end_endpoint_type: Mapped[str | None] = mapped_column(String(40))
    end_endpoint_ref_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))

    latitude: Mapped[float | None] = mapped_column(Float)
    longitude: Mapped[float | None] = mapped_column(Float)
    accuracy_m: Mapped[float | None] = mapped_column(Float)
    instrument: Mapped[str | None] = mapped_column(String(120))
    measurement_payload: Mapped[dict] = mapped_column(
        JSON, nullable=False, default=dict
    )
    attachment_ids: Mapped[list] = mapped_column(JSON, nullable=False, default=list)
    notes: Mapped[str | None] = mapped_column(Text)

    claim_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    observation_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    client_ref: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    recorded_by_technician_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("technician_profiles.id", ondelete="RESTRICT"),
        nullable=False,
    )
    recorded_by_person_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), nullable=False
    )
    recorded_by_system_user_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("system_users.id", ondelete="RESTRICT"),
    )
    observed_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(UTC), nullable=False
    )

    staged_feature = relationship("FiberTopologyStagedFeature")
    work_order = relationship("WorkOrder")
    recorded_by_technician = relationship("TechnicianProfile")
    recorded_by_system_user = relationship("SystemUser")


__all__ = ["FiberTopologyFieldObservation"]
