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
from sqlalchemy.orm import Mapped, mapped_column

from app.db import Base


class OntTopologyObservationEvidence(Base):
    """Durable projection of one distinct observed ONT electronic location."""

    __tablename__ = "ont_topology_observation_evidence"
    __table_args__ = (
        UniqueConstraint(
            "observation_sha256",
            name="uq_ont_topology_observation_sha256",
        ),
        Index(
            "ix_ont_topology_observation_ont_source",
            "ont_unit_id",
            "source",
        ),
        Index(
            "ix_ont_topology_observation_latest_outcome",
            "latest_outcome",
        ),
        CheckConstraint(
            "initial_outcome IN "
            "('initialized', 'confirmed', 'incomplete', 'review_required')",
            name="ck_ont_topology_observation_initial_outcome",
        ),
        CheckConstraint(
            "latest_outcome IN "
            "('initialized', 'confirmed', 'incomplete', 'review_required')",
            name="ck_ont_topology_observation_latest_outcome",
        ),
        CheckConstraint(
            "seen_count > 0",
            name="ck_ont_topology_observation_seen_count",
        ),
        CheckConstraint(
            "length(observation_sha256) = 64",
            name="ck_ont_topology_observation_sha256",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    source: Mapped[str] = mapped_column(String(40), nullable=False)
    evidence_key: Mapped[str] = mapped_column(String(200), nullable=False)
    observation_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    ont_unit_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("ont_units.id", ondelete="RESTRICT"),
        nullable=False,
    )
    observed_olt_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("olt_devices.id", ondelete="RESTRICT"),
        nullable=False,
    )
    observed_pon_port_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("pon_ports.id", ondelete="RESTRICT"),
    )
    observed_port_number: Mapped[int | None] = mapped_column(Integer)
    observed_port_label: Mapped[str | None] = mapped_column(String(120))
    canonical_olt_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("olt_devices.id", ondelete="RESTRICT"),
    )
    canonical_pon_port_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("pon_ports.id", ondelete="RESTRICT"),
    )
    active_assignment_ids: Mapped[list[str]] = mapped_column(JSON, nullable=False)
    assignment_conflict_ids: Mapped[list[str]] = mapped_column(JSON, nullable=False)
    initial_outcome: Mapped[str] = mapped_column(String(24), nullable=False)
    latest_outcome: Mapped[str] = mapped_column(String(24), nullable=False)
    latest_reason: Mapped[str | None] = mapped_column(String(500))
    initial_result: Mapped[dict[str, object]] = mapped_column(JSON, nullable=False)
    latest_snapshot: Mapped[dict[str, object]] = mapped_column(JSON, nullable=False)
    first_seen_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(UTC), nullable=False
    )
    last_seen_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(UTC), nullable=False
    )
    seen_count: Mapped[int] = mapped_column(Integer, default=1, nullable=False)
    resolved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
