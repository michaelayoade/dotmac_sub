"""Canonical rack, optical connector, and fiber-core continuity models."""

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


class FiberRack(Base):
    """A fiber rack hosted by one exact canonical infrastructure asset."""

    __tablename__ = "fiber_racks"
    __table_args__ = (
        UniqueConstraint("code", name="uq_fiber_racks_code"),
        CheckConstraint("rack_units > 0", name="ck_fiber_racks_positive_units"),
        CheckConstraint(
            "(pop_site_id IS NOT NULL AND fdh_cabinet_id IS NULL "
            "AND fiber_access_point_id IS NULL AND splice_closure_id IS NULL) OR "
            "(pop_site_id IS NULL AND fdh_cabinet_id IS NOT NULL "
            "AND fiber_access_point_id IS NULL AND splice_closure_id IS NULL) OR "
            "(pop_site_id IS NULL AND fdh_cabinet_id IS NULL "
            "AND fiber_access_point_id IS NOT NULL AND splice_closure_id IS NULL) OR "
            "(pop_site_id IS NULL AND fdh_cabinet_id IS NULL "
            "AND fiber_access_point_id IS NULL AND splice_closure_id IS NOT NULL)",
            name="ck_fiber_racks_exact_host",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    code: Mapped[str] = mapped_column(String(80), nullable=False)
    name: Mapped[str] = mapped_column(String(160), nullable=False)
    pop_site_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("pop_sites.id", ondelete="RESTRICT")
    )
    fdh_cabinet_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("fdh_cabinets.id", ondelete="RESTRICT")
    )
    fiber_access_point_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("fiber_access_points.id", ondelete="RESTRICT")
    )
    splice_closure_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("fiber_splice_closures.id", ondelete="RESTRICT"),
    )
    rack_units: Mapped[int] = mapped_column(Integer, nullable=False)
    notes: Mapped[str | None] = mapped_column(Text)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(UTC), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(UTC),
        onupdate=lambda: datetime.now(UTC),
        nullable=False,
    )


class FiberPatchPanel(Base):
    """An ODF, patch panel, or splice panel mounted in an exact rack position."""

    __tablename__ = "fiber_patch_panels"
    __table_args__ = (
        UniqueConstraint("rack_id", "name", name="uq_fiber_patch_panels_rack_name"),
        UniqueConstraint(
            "rack_id",
            "rack_unit_start",
            name="uq_fiber_patch_panels_rack_unit_start",
        ),
        CheckConstraint(
            "panel_type IN ('odf', 'patch_panel', 'splice_panel')",
            name="ck_fiber_patch_panels_type",
        ),
        CheckConstraint(
            "rack_unit_start > 0 AND rack_unit_height > 0",
            name="ck_fiber_patch_panels_positive_units",
        ),
        CheckConstraint(
            "port_capacity > 0", name="ck_fiber_patch_panels_positive_capacity"
        ),
        CheckConstraint(
            "connector_type IN ('sc', 'lc', 'fc', 'st')",
            name="ck_fiber_patch_panels_connector_type",
        ),
        CheckConstraint(
            "polish_type IN ('apc', 'upc', 'pc')",
            name="ck_fiber_patch_panels_polish_type",
        ),
        CheckConstraint(
            "fiber_mode IN ('single_mode', 'multi_mode')",
            name="ck_fiber_patch_panels_fiber_mode",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    rack_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("fiber_racks.id", ondelete="RESTRICT"),
        nullable=False,
    )
    name: Mapped[str] = mapped_column(String(160), nullable=False)
    panel_type: Mapped[str] = mapped_column(String(24), nullable=False)
    rack_unit_start: Mapped[int] = mapped_column(Integer, nullable=False)
    rack_unit_height: Mapped[int] = mapped_column(Integer, default=1, nullable=False)
    port_capacity: Mapped[int] = mapped_column(Integer, nullable=False)
    connector_type: Mapped[str] = mapped_column(String(16), nullable=False)
    polish_type: Mapped[str] = mapped_column(String(16), nullable=False)
    fiber_mode: Mapped[str] = mapped_column(String(24), nullable=False)
    notes: Mapped[str | None] = mapped_column(Text)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(UTC), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(UTC),
        onupdate=lambda: datetime.now(UTC),
        nullable=False,
    )


class FiberConnectorPort(Base):
    """One optical channel on a panel, PON, splitter port, or ONT."""

    __tablename__ = "fiber_connector_ports"
    __table_args__ = (
        UniqueConstraint(
            "patch_panel_id",
            "port_number",
            name="uq_fiber_connector_ports_panel_number",
        ),
        UniqueConstraint("pon_port_id", name="uq_fiber_connector_ports_pon"),
        UniqueConstraint("splitter_port_id", name="uq_fiber_connector_ports_splitter"),
        UniqueConstraint("ont_unit_id", name="uq_fiber_connector_ports_ont"),
        CheckConstraint(
            "(patch_panel_id IS NOT NULL AND pon_port_id IS NULL "
            "AND splitter_port_id IS NULL AND ont_unit_id IS NULL "
            "AND port_number IS NOT NULL AND port_number > 0) OR "
            "(patch_panel_id IS NULL AND pon_port_id IS NOT NULL "
            "AND splitter_port_id IS NULL AND ont_unit_id IS NULL "
            "AND port_number IS NULL) OR "
            "(patch_panel_id IS NULL AND pon_port_id IS NULL "
            "AND splitter_port_id IS NOT NULL AND ont_unit_id IS NULL "
            "AND port_number IS NULL) OR "
            "(patch_panel_id IS NULL AND pon_port_id IS NULL "
            "AND splitter_port_id IS NULL AND ont_unit_id IS NOT NULL "
            "AND port_number IS NULL)",
            name="ck_fiber_connector_ports_exact_owner",
        ),
        CheckConstraint(
            "connector_type IN ('sc', 'lc', 'fc', 'st')",
            name="ck_fiber_connector_ports_connector_type",
        ),
        CheckConstraint(
            "polish_type IN ('apc', 'upc', 'pc')",
            name="ck_fiber_connector_ports_polish_type",
        ),
        CheckConstraint(
            "fiber_mode IN ('single_mode', 'multi_mode')",
            name="ck_fiber_connector_ports_fiber_mode",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    patch_panel_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("fiber_patch_panels.id", ondelete="RESTRICT"),
    )
    pon_port_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("pon_ports.id", ondelete="RESTRICT")
    )
    splitter_port_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("splitter_ports.id", ondelete="RESTRICT")
    )
    ont_unit_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("ont_units.id", ondelete="RESTRICT")
    )
    port_number: Mapped[int | None] = mapped_column(Integer)
    label: Mapped[str | None] = mapped_column(String(160))
    connector_type: Mapped[str] = mapped_column(String(16), nullable=False)
    polish_type: Mapped[str] = mapped_column(String(16), nullable=False)
    fiber_mode: Mapped[str] = mapped_column(String(24), nullable=False)
    notes: Mapped[str | None] = mapped_column(Text)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(UTC), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(UTC),
        onupdate=lambda: datetime.now(UTC),
        nullable=False,
    )


class FiberPhysicalLinkDecision(Base):
    """Independently reviewed exact splice, termination, or patch-cord change."""

    __tablename__ = "fiber_physical_link_decisions"
    __table_args__ = (
        UniqueConstraint(
            "decision_sha256", name="uq_fiber_physical_link_decision_sha256"
        ),
        Index("ix_fiber_physical_link_decisions_status", "status"),
        CheckConstraint(
            "link_type IN ('core_splice', 'strand_termination', 'patch_cord')",
            name="ck_fiber_physical_link_decisions_type",
        ),
        CheckConstraint(
            "action IN ('connect', 'disconnect')",
            name="ck_fiber_physical_link_decisions_action",
        ),
        CheckConstraint(
            "status IN ('proposed', 'approved', 'declined', 'applied', 'closed')",
            name="ck_fiber_physical_link_decisions_status",
        ),
        CheckConstraint(
            "(action = 'connect' AND target_link_id IS NULL) OR "
            "(action = 'disconnect' AND target_link_id IS NOT NULL)",
            name="ck_fiber_physical_link_decisions_target",
        ),
        CheckConstraint(
            "reviewed_by IS NULL OR reviewed_by <> proposed_by",
            name="ck_fiber_physical_link_decisions_separation",
        ),
        CheckConstraint(
            "(status = 'proposed' AND reviewed_by IS NULL AND reviewed_at IS NULL) OR "
            "(status <> 'proposed' AND reviewed_by IS NOT NULL "
            "AND reviewed_at IS NOT NULL)",
            name="ck_fiber_physical_link_decisions_review_evidence",
        ),
        CheckConstraint(
            "(status IN ('applied', 'closed') AND executed_by IS NOT NULL "
            "AND executed_at IS NOT NULL AND result_payload IS NOT NULL "
            "AND result_sha256 IS NOT NULL) OR "
            "(status NOT IN ('applied', 'closed') AND executed_by IS NULL "
            "AND executed_at IS NULL AND result_payload IS NULL "
            "AND result_sha256 IS NULL)",
            name="ck_fiber_physical_link_decisions_result_evidence",
        ),
        CheckConstraint(
            "length(decision_sha256) = 64",
            name="ck_fiber_physical_link_decisions_digest",
        ),
        CheckConstraint(
            "result_sha256 IS NULL OR length(result_sha256) = 64",
            name="ck_fiber_physical_link_decisions_result_digest",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    link_type: Mapped[str] = mapped_column(String(32), nullable=False)
    action: Mapped[str] = mapped_column(String(16), nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False)
    target_link_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    first_strand_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("fiber_strands.id", ondelete="RESTRICT")
    )
    first_strand_end: Mapped[str | None] = mapped_column(String(1))
    second_strand_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("fiber_strands.id", ondelete="RESTRICT")
    )
    second_strand_end: Mapped[str | None] = mapped_column(String(1))
    connector_port_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("fiber_connector_ports.id", ondelete="RESTRICT"),
    )
    first_connector_port_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("fiber_connector_ports.id", ondelete="RESTRICT"),
    )
    second_connector_port_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("fiber_connector_ports.id", ondelete="RESTRICT"),
    )
    splice_closure_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("fiber_splice_closures.id", ondelete="RESTRICT"),
    )
    splice_tray_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("fiber_splice_trays.id", ondelete="RESTRICT"),
    )
    position: Mapped[int | None] = mapped_column(Integer)
    splice_type: Mapped[str | None] = mapped_column(String(80))
    label: Mapped[str | None] = mapped_column(String(160))
    assembly_label: Mapped[str | None] = mapped_column(String(160))
    length_m: Mapped[Decimal | None] = mapped_column(Numeric(12, 3))
    insertion_loss_db: Mapped[Decimal | None] = mapped_column(Numeric(8, 3))
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


class FiberCoreSplice(Base):
    """Canonical reviewed join between two exact cable-core ends."""

    __tablename__ = "fiber_core_splices"
    __table_args__ = (
        UniqueConstraint(
            "created_by_decision_id", name="uq_fiber_core_splices_create_decision"
        ),
        UniqueConstraint(
            "retired_by_decision_id", name="uq_fiber_core_splices_retire_decision"
        ),
        CheckConstraint(
            "first_strand_id <> second_strand_id",
            name="ck_fiber_core_splices_distinct_strands",
        ),
        CheckConstraint(
            "first_strand_end IN ('a', 'b') AND second_strand_end IN ('a', 'b')",
            name="ck_fiber_core_splices_ends",
        ),
        CheckConstraint(
            "(active AND retired_by_decision_id IS NULL) OR "
            "(NOT active AND retired_by_decision_id IS NOT NULL)",
            name="ck_fiber_core_splices_retirement",
        ),
        CheckConstraint(
            "position IS NULL OR position > 0",
            name="ck_fiber_core_splices_position",
        ),
        CheckConstraint(
            "insertion_loss_db IS NULL OR insertion_loss_db >= 0",
            name="ck_fiber_core_splices_loss",
        ),
        Index("ix_fiber_core_splices_closure", "splice_closure_id"),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    first_strand_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("fiber_strands.id", ondelete="RESTRICT"),
        nullable=False,
    )
    first_strand_end: Mapped[str] = mapped_column(String(1), nullable=False)
    second_strand_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("fiber_strands.id", ondelete="RESTRICT"),
        nullable=False,
    )
    second_strand_end: Mapped[str] = mapped_column(String(1), nullable=False)
    splice_closure_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("fiber_splice_closures.id", ondelete="RESTRICT"),
        nullable=False,
    )
    splice_tray_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("fiber_splice_trays.id", ondelete="RESTRICT"),
    )
    position: Mapped[int | None] = mapped_column(Integer)
    splice_type: Mapped[str] = mapped_column(String(80), nullable=False)
    insertion_loss_db: Mapped[Decimal | None] = mapped_column(Numeric(8, 3))
    created_by_decision_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("fiber_physical_link_decisions.id", ondelete="RESTRICT"),
        nullable=False,
    )
    retired_by_decision_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("fiber_physical_link_decisions.id", ondelete="RESTRICT"),
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


class FiberStrandTermination(Base):
    """Canonical reviewed landing of one exact cable-core end on a connector."""

    __tablename__ = "fiber_strand_terminations"
    __table_args__ = (
        UniqueConstraint(
            "created_by_decision_id",
            name="uq_fiber_strand_terminations_create_decision",
        ),
        UniqueConstraint(
            "retired_by_decision_id",
            name="uq_fiber_strand_terminations_retire_decision",
        ),
        CheckConstraint(
            "strand_end IN ('a', 'b')",
            name="ck_fiber_strand_terminations_end",
        ),
        CheckConstraint(
            "(active AND retired_by_decision_id IS NULL) OR "
            "(NOT active AND retired_by_decision_id IS NOT NULL)",
            name="ck_fiber_strand_terminations_retirement",
        ),
        Index(
            "uq_fiber_strand_terminations_active_end",
            "strand_id",
            "strand_end",
            unique=True,
            postgresql_where=text("active"),
            sqlite_where=text("active"),
        ),
        Index(
            "uq_fiber_strand_terminations_active_connector",
            "connector_port_id",
            unique=True,
            postgresql_where=text("active"),
            sqlite_where=text("active"),
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    strand_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("fiber_strands.id", ondelete="RESTRICT"),
        nullable=False,
    )
    strand_end: Mapped[str] = mapped_column(String(1), nullable=False)
    connector_port_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("fiber_connector_ports.id", ondelete="RESTRICT"),
        nullable=False,
    )
    created_by_decision_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("fiber_physical_link_decisions.id", ondelete="RESTRICT"),
        nullable=False,
    )
    retired_by_decision_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("fiber_physical_link_decisions.id", ondelete="RESTRICT"),
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


class FiberPatchCord(Base):
    """Canonical reviewed one-channel optical patch between exact connectors."""

    __tablename__ = "fiber_patch_cords"
    __table_args__ = (
        UniqueConstraint(
            "created_by_decision_id", name="uq_fiber_patch_cords_create_decision"
        ),
        UniqueConstraint(
            "retired_by_decision_id", name="uq_fiber_patch_cords_retire_decision"
        ),
        CheckConstraint(
            "first_connector_port_id <> second_connector_port_id",
            name="ck_fiber_patch_cords_distinct_connectors",
        ),
        CheckConstraint(
            "(active AND retired_by_decision_id IS NULL) OR "
            "(NOT active AND retired_by_decision_id IS NOT NULL)",
            name="ck_fiber_patch_cords_retirement",
        ),
        CheckConstraint(
            "length_m IS NULL OR length_m > 0",
            name="ck_fiber_patch_cords_length",
        ),
        CheckConstraint(
            "insertion_loss_db IS NULL OR insertion_loss_db >= 0",
            name="ck_fiber_patch_cords_loss",
        ),
        Index(
            "uq_fiber_patch_cords_active_first_connector",
            "first_connector_port_id",
            unique=True,
            postgresql_where=text("active"),
            sqlite_where=text("active"),
        ),
        Index(
            "uq_fiber_patch_cords_active_second_connector",
            "second_connector_port_id",
            unique=True,
            postgresql_where=text("active"),
            sqlite_where=text("active"),
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    first_connector_port_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("fiber_connector_ports.id", ondelete="RESTRICT"),
        nullable=False,
    )
    second_connector_port_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("fiber_connector_ports.id", ondelete="RESTRICT"),
        nullable=False,
    )
    label: Mapped[str] = mapped_column(String(160), nullable=False)
    assembly_label: Mapped[str | None] = mapped_column(String(160))
    length_m: Mapped[Decimal | None] = mapped_column(Numeric(12, 3))
    insertion_loss_db: Mapped[Decimal | None] = mapped_column(Numeric(8, 3))
    created_by_decision_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("fiber_physical_link_decisions.id", ondelete="RESTRICT"),
        nullable=False,
    )
    retired_by_decision_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("fiber_physical_link_decisions.id", ondelete="RESTRICT"),
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
