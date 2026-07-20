from __future__ import annotations

import uuid
from datetime import UTC, datetime

from geoalchemy2 import Geometry
from sqlalchemy import (
    JSON,
    Boolean,
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


class FiberSupportStructure(Base):
    """Canonical Sub identity and operational state for a physical support."""

    __tablename__ = "fiber_support_structures"
    __table_args__ = (
        UniqueConstraint("code", name="uq_fiber_support_structures_code"),
        CheckConstraint(
            "support_type IN ('pole', 'tower', 'building_attachment', 'other')",
            name="ck_fiber_support_structures_type",
        ),
        CheckConstraint(
            "ownership_status IN ('unknown', 'dotmac_owned', 'leased', 'third_party')",
            name="ck_fiber_support_structures_ownership",
        ),
        CheckConstraint(
            "lifecycle_status IN ('planned', 'active', 'suspended', 'retired')",
            name="ck_fiber_support_structures_lifecycle",
        ),
        CheckConstraint(
            "inspection_status IN ('uninspected', 'due', 'passed', 'conditional', 'failed')",
            name="ck_fiber_support_structures_inspection",
        ),
        CheckConstraint(
            "lease_status IN ('unknown', 'not_required', 'pending', 'active', 'expired', 'terminated')",
            name="ck_fiber_support_structures_lease",
        ),
        Index("ix_fiber_support_structures_lifecycle", "lifecycle_status"),
        Index("ix_fiber_support_structures_inspection", "inspection_status"),
        Index("ix_fiber_support_structures_lease", "lease_status"),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    code: Mapped[str] = mapped_column(String(80), nullable=False)
    name: Mapped[str] = mapped_column(String(160), nullable=False)
    support_type: Mapped[str] = mapped_column(
        String(40), default="pole", nullable=False
    )
    owner_name: Mapped[str | None] = mapped_column(String(160))
    ownership_status: Mapped[str] = mapped_column(
        String(30), default="unknown", nullable=False
    )
    lifecycle_status: Mapped[str] = mapped_column(
        String(30), default="active", nullable=False
    )
    inspection_status: Mapped[str] = mapped_column(
        String(30), default="uninspected", nullable=False
    )
    last_inspected_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    next_inspection_due_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True)
    )
    lease_status: Mapped[str] = mapped_column(
        String(30), default="unknown", nullable=False
    )
    lease_reference: Mapped[str | None] = mapped_column(String(160))
    lease_starts_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    lease_ends_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    latitude: Mapped[float | None] = mapped_column()
    longitude: Mapped[float | None] = mapped_column()
    geom = mapped_column(Geometry("POINT", srid=4326), nullable=True)
    notes: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(UTC)
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(UTC),
        onupdate=lambda: datetime.now(UTC),
    )

    mounts = relationship("FiberSupportMount", back_populates="support_structure")

    @property
    def is_active(self) -> bool:
        return self.lifecycle_status == "active"


class FiberSupportMountDecision(Base):
    """Immutable reviewed command evidence for one exact support mount edge."""

    __tablename__ = "fiber_support_mount_decisions"
    __table_args__ = (
        UniqueConstraint(
            "decision_sha256", name="uq_fiber_support_mount_decisions_digest"
        ),
        CheckConstraint(
            "action IN ('attach', 'detach')",
            name="ck_fiber_support_mount_decisions_action",
        ),
        CheckConstraint(
            "mounted_asset_type IN ('fdh_cabinet', 'fiber_access_point', 'splice_closure', 'fiber_segment')",
            name="ck_fiber_support_mount_decisions_asset_type",
        ),
        CheckConstraint(
            "mount_role IN ('hosted', 'route_support', 'anchor')",
            name="ck_fiber_support_mount_decisions_role",
        ),
        CheckConstraint(
            "status IN ('proposed', 'approved', 'declined', 'applied', 'closed')",
            name="ck_fiber_support_mount_decisions_status",
        ),
        CheckConstraint(
            "(action = 'attach' AND existing_mount_id IS NULL) OR "
            "(action = 'detach' AND existing_mount_id IS NOT NULL)",
            name="ck_fiber_support_mount_decisions_existing_mount",
        ),
        CheckConstraint(
            "(mounted_asset_type = 'fiber_segment' "
            "AND mount_role IN ('route_support', 'anchor') "
            "AND sequence IS NOT NULL AND sequence > 0) OR "
            "(mounted_asset_type <> 'fiber_segment' "
            "AND mount_role = 'hosted' AND sequence IS NULL)",
            name="ck_fiber_support_mount_decisions_shape",
        ),
        CheckConstraint(
            "reviewed_by IS NULL OR reviewed_by <> proposed_by",
            name="ck_fiber_support_mount_decisions_review_separation",
        ),
        CheckConstraint(
            "(status = 'proposed' AND reviewed_by IS NULL AND reviewed_at IS NULL) OR "
            "(status <> 'proposed' AND reviewed_by IS NOT NULL AND reviewed_at IS NOT NULL)",
            name="ck_fiber_support_mount_decisions_review_evidence",
        ),
        CheckConstraint(
            "(status IN ('applied', 'closed') AND executed_by IS NOT NULL "
            "AND executed_at IS NOT NULL AND result_payload IS NOT NULL "
            "AND result_sha256 IS NOT NULL) OR "
            "(status NOT IN ('applied', 'closed') AND executed_by IS NULL "
            "AND executed_at IS NULL AND result_payload IS NULL "
            "AND result_sha256 IS NULL)",
            name="ck_fiber_support_mount_decisions_result_evidence",
        ),
        CheckConstraint(
            "(status = 'applied' AND result_mount_id IS NOT NULL) OR "
            "status <> 'applied'",
            name="ck_fiber_support_mount_decisions_applied_mount",
        ),
        CheckConstraint(
            "length(decision_sha256) = 64 AND "
            "length(expected_support_state_sha256) = 64 AND "
            "length(expected_asset_state_sha256) = 64 AND "
            "(expected_mount_state_sha256 IS NULL OR "
            "length(expected_mount_state_sha256) = 64) AND "
            "(result_sha256 IS NULL OR length(result_sha256) = 64)",
            name="ck_fiber_support_mount_decisions_digests",
        ),
        Index("ix_fiber_support_mount_decisions_status", "status"),
        Index(
            "uq_fiber_support_mount_decisions_active_asset",
            "mounted_asset_type",
            "mounted_asset_id",
            unique=True,
            postgresql_where=text("status IN ('proposed', 'approved')"),
            sqlite_where=text("status IN ('proposed', 'approved')"),
        ),
        Index(
            "ix_fiber_support_mount_decisions_asset",
            "mounted_asset_type",
            "mounted_asset_id",
        ),
        Index("ix_fiber_support_mount_decisions_support", "support_structure_id"),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    action: Mapped[str] = mapped_column(String(20), nullable=False)
    support_structure_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("fiber_support_structures.id"),
        nullable=False,
    )
    mounted_asset_type: Mapped[str] = mapped_column(String(40), nullable=False)
    mounted_asset_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), nullable=False
    )
    mount_role: Mapped[str] = mapped_column(String(30), nullable=False)
    sequence: Mapped[int | None] = mapped_column(Integer)
    existing_mount_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    expected_support_state_sha256: Mapped[str] = mapped_column(
        String(64), nullable=False
    )
    expected_asset_state_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    expected_mount_state_sha256: Mapped[str | None] = mapped_column(String(64))
    reason: Mapped[str] = mapped_column(Text, nullable=False)
    proposed_by: Mapped[str] = mapped_column(String(160), nullable=False)
    proposed_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(UTC)
    )
    reviewed_by: Mapped[str | None] = mapped_column(String(160))
    review_notes: Mapped[str | None] = mapped_column(Text)
    reviewed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    executed_by: Mapped[str | None] = mapped_column(String(160))
    executed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    status: Mapped[str] = mapped_column(String(20), default="proposed", nullable=False)
    closed_reason: Mapped[str | None] = mapped_column(String(160))
    decision_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    result_mount_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    result_payload: Mapped[dict[str, object] | None] = mapped_column(JSON)
    result_sha256: Mapped[str | None] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(UTC)
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(UTC),
        onupdate=lambda: datetime.now(UTC),
    )


class FiberSupportMount(Base):
    """One exact canonical asset-to-support edge."""

    __tablename__ = "fiber_support_mounts"
    __table_args__ = (
        UniqueConstraint("decision_id", name="uq_fiber_support_mounts_decision"),
        CheckConstraint(
            "mounted_asset_type IN ('fdh_cabinet', 'fiber_access_point', 'splice_closure', 'fiber_segment')",
            name="ck_fiber_support_mounts_asset_type",
        ),
        CheckConstraint(
            "mount_role IN ('hosted', 'route_support', 'anchor')",
            name="ck_fiber_support_mounts_role",
        ),
        CheckConstraint(
            "(mounted_asset_type = 'fiber_segment' "
            "AND mount_role IN ('route_support', 'anchor') "
            "AND sequence IS NOT NULL AND sequence > 0) OR "
            "(mounted_asset_type <> 'fiber_segment' "
            "AND mount_role = 'hosted' AND sequence IS NULL)",
            name="ck_fiber_support_mounts_shape",
        ),
        Index(
            "uq_fiber_support_mounts_active_edge",
            "support_structure_id",
            "mounted_asset_type",
            "mounted_asset_id",
            unique=True,
            postgresql_where=text("is_active"),
            sqlite_where=text("is_active = 1"),
        ),
        Index(
            "uq_fiber_support_mounts_active_point_asset",
            "mounted_asset_type",
            "mounted_asset_id",
            unique=True,
            postgresql_where=text(
                "is_active AND mounted_asset_type <> 'fiber_segment'"
            ),
            sqlite_where=text(
                "is_active = 1 AND mounted_asset_type <> 'fiber_segment'"
            ),
        ),
        Index(
            "uq_fiber_support_mounts_active_segment_sequence",
            "mounted_asset_id",
            "sequence",
            unique=True,
            postgresql_where=text("is_active AND mounted_asset_type = 'fiber_segment'"),
            sqlite_where=text("is_active = 1 AND mounted_asset_type = 'fiber_segment'"),
        ),
        Index("ix_fiber_support_mounts_support", "support_structure_id"),
        Index(
            "ix_fiber_support_mounts_asset", "mounted_asset_type", "mounted_asset_id"
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    decision_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("fiber_support_mount_decisions.id"),
        nullable=False,
    )
    support_structure_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("fiber_support_structures.id"),
        nullable=False,
    )
    mounted_asset_type: Mapped[str] = mapped_column(String(40), nullable=False)
    mounted_asset_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), nullable=False
    )
    mount_role: Mapped[str] = mapped_column(String(30), nullable=False)
    sequence: Mapped[int | None] = mapped_column(Integer)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    installed_by: Mapped[str] = mapped_column(String(160), nullable=False)
    installed_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(UTC)
    )
    removed_by: Mapped[str | None] = mapped_column(String(160))
    removed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    notes: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(UTC)
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(UTC),
        onupdate=lambda: datetime.now(UTC),
    )

    support_structure = relationship("FiberSupportStructure", back_populates="mounts")
