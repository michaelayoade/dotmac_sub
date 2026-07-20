"""Add racks, ODF/patching, and reviewed exact fiber-core continuity.

Revision ID: 364_fiber_core_continuity
Revises: 363_fiber_plant_operational_integrity
Create Date: 2026-07-18

This migration creates empty canonical inventory and decision tables. It does
not infer racks, panels, ports, patches, terminations, or splices from names,
geometry, legacy scalar fields, or imported labels.
"""

from __future__ import annotations

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision = "364_fiber_core_continuity"
down_revision = "363_fiber_plant_operational_integrity"
branch_labels = None
depends_on = None


def _timestamps() -> tuple[sa.Column, sa.Column]:
    return (
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
    )


def upgrade() -> None:
    op.create_table(
        "fiber_racks",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("code", sa.String(length=80), nullable=False),
        sa.Column("name", sa.String(length=160), nullable=False),
        sa.Column("pop_site_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("fdh_cabinet_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column(
            "fiber_access_point_id", postgresql.UUID(as_uuid=True), nullable=True
        ),
        sa.Column("splice_closure_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("rack_units", sa.Integer(), nullable=False),
        sa.Column("notes", sa.Text(), nullable=True),
        sa.Column("is_active", sa.Boolean(), nullable=False),
        *_timestamps(),
        sa.CheckConstraint("rack_units > 0", name="ck_fiber_racks_positive_units"),
        sa.CheckConstraint(
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
        sa.ForeignKeyConstraint(["pop_site_id"], ["pop_sites.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(
            ["fdh_cabinet_id"], ["fdh_cabinets.id"], ondelete="RESTRICT"
        ),
        sa.ForeignKeyConstraint(
            ["fiber_access_point_id"],
            ["fiber_access_points.id"],
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["splice_closure_id"],
            ["fiber_splice_closures.id"],
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("code", name="uq_fiber_racks_code"),
    )

    op.create_table(
        "fiber_patch_panels",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("rack_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("name", sa.String(length=160), nullable=False),
        sa.Column("panel_type", sa.String(length=24), nullable=False),
        sa.Column("rack_unit_start", sa.Integer(), nullable=False),
        sa.Column("rack_unit_height", sa.Integer(), nullable=False),
        sa.Column("port_capacity", sa.Integer(), nullable=False),
        sa.Column("connector_type", sa.String(length=16), nullable=False),
        sa.Column("polish_type", sa.String(length=16), nullable=False),
        sa.Column("fiber_mode", sa.String(length=24), nullable=False),
        sa.Column("notes", sa.Text(), nullable=True),
        sa.Column("is_active", sa.Boolean(), nullable=False),
        *_timestamps(),
        sa.CheckConstraint(
            "panel_type IN ('odf', 'patch_panel', 'splice_panel')",
            name="ck_fiber_patch_panels_type",
        ),
        sa.CheckConstraint(
            "rack_unit_start > 0 AND rack_unit_height > 0",
            name="ck_fiber_patch_panels_positive_units",
        ),
        sa.CheckConstraint(
            "port_capacity > 0", name="ck_fiber_patch_panels_positive_capacity"
        ),
        sa.CheckConstraint(
            "connector_type IN ('sc', 'lc', 'fc', 'st')",
            name="ck_fiber_patch_panels_connector_type",
        ),
        sa.CheckConstraint(
            "polish_type IN ('apc', 'upc', 'pc')",
            name="ck_fiber_patch_panels_polish_type",
        ),
        sa.CheckConstraint(
            "fiber_mode IN ('single_mode', 'multi_mode')",
            name="ck_fiber_patch_panels_fiber_mode",
        ),
        sa.ForeignKeyConstraint(["rack_id"], ["fiber_racks.id"], ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("rack_id", "name", name="uq_fiber_patch_panels_rack_name"),
        sa.UniqueConstraint(
            "rack_id",
            "rack_unit_start",
            name="uq_fiber_patch_panels_rack_unit_start",
        ),
    )

    op.create_table(
        "fiber_connector_ports",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("patch_panel_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("pon_port_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("splitter_port_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("ont_unit_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("port_number", sa.Integer(), nullable=True),
        sa.Column("label", sa.String(length=160), nullable=True),
        sa.Column("connector_type", sa.String(length=16), nullable=False),
        sa.Column("polish_type", sa.String(length=16), nullable=False),
        sa.Column("fiber_mode", sa.String(length=24), nullable=False),
        sa.Column("notes", sa.Text(), nullable=True),
        sa.Column("is_active", sa.Boolean(), nullable=False),
        *_timestamps(),
        sa.CheckConstraint(
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
        sa.CheckConstraint(
            "connector_type IN ('sc', 'lc', 'fc', 'st')",
            name="ck_fiber_connector_ports_connector_type",
        ),
        sa.CheckConstraint(
            "polish_type IN ('apc', 'upc', 'pc')",
            name="ck_fiber_connector_ports_polish_type",
        ),
        sa.CheckConstraint(
            "fiber_mode IN ('single_mode', 'multi_mode')",
            name="ck_fiber_connector_ports_fiber_mode",
        ),
        sa.ForeignKeyConstraint(
            ["patch_panel_id"], ["fiber_patch_panels.id"], ondelete="RESTRICT"
        ),
        sa.ForeignKeyConstraint(["pon_port_id"], ["pon_ports.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(
            ["splitter_port_id"], ["splitter_ports.id"], ondelete="RESTRICT"
        ),
        sa.ForeignKeyConstraint(["ont_unit_id"], ["ont_units.id"], ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "patch_panel_id",
            "port_number",
            name="uq_fiber_connector_ports_panel_number",
        ),
        sa.UniqueConstraint("pon_port_id", name="uq_fiber_connector_ports_pon"),
        sa.UniqueConstraint(
            "splitter_port_id", name="uq_fiber_connector_ports_splitter"
        ),
        sa.UniqueConstraint("ont_unit_id", name="uq_fiber_connector_ports_ont"),
    )

    op.create_table(
        "fiber_physical_link_decisions",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("link_type", sa.String(length=32), nullable=False),
        sa.Column("action", sa.String(length=16), nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("target_link_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("first_strand_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("first_strand_end", sa.String(length=1), nullable=True),
        sa.Column("second_strand_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("second_strand_end", sa.String(length=1), nullable=True),
        sa.Column("connector_port_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column(
            "first_connector_port_id", postgresql.UUID(as_uuid=True), nullable=True
        ),
        sa.Column(
            "second_connector_port_id", postgresql.UUID(as_uuid=True), nullable=True
        ),
        sa.Column("splice_closure_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("splice_tray_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("position", sa.Integer(), nullable=True),
        sa.Column("splice_type", sa.String(length=80), nullable=True),
        sa.Column("label", sa.String(length=160), nullable=True),
        sa.Column("assembly_label", sa.String(length=160), nullable=True),
        sa.Column("length_m", sa.Numeric(precision=12, scale=3), nullable=True),
        sa.Column("insertion_loss_db", sa.Numeric(precision=8, scale=3), nullable=True),
        sa.Column("reason", sa.Text(), nullable=False),
        sa.Column("decision_sha256", sa.String(length=64), nullable=False),
        sa.Column("proposed_by", sa.String(length=160), nullable=False),
        sa.Column("proposed_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("reviewed_by", sa.String(length=160), nullable=True),
        sa.Column("review_notes", sa.Text(), nullable=True),
        sa.Column("reviewed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("executed_by", sa.String(length=160), nullable=True),
        sa.Column("executed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("closed_reason", sa.Text(), nullable=True),
        sa.Column("result_payload", sa.JSON(), nullable=True),
        sa.Column("result_sha256", sa.String(length=64), nullable=True),
        sa.CheckConstraint(
            "link_type IN ('core_splice', 'strand_termination', 'patch_cord')",
            name="ck_fiber_physical_link_decisions_type",
        ),
        sa.CheckConstraint(
            "action IN ('connect', 'disconnect')",
            name="ck_fiber_physical_link_decisions_action",
        ),
        sa.CheckConstraint(
            "status IN ('proposed', 'approved', 'declined', 'applied', 'closed')",
            name="ck_fiber_physical_link_decisions_status",
        ),
        sa.CheckConstraint(
            "(action = 'connect' AND target_link_id IS NULL) OR "
            "(action = 'disconnect' AND target_link_id IS NOT NULL)",
            name="ck_fiber_physical_link_decisions_target",
        ),
        sa.CheckConstraint(
            "reviewed_by IS NULL OR reviewed_by <> proposed_by",
            name="ck_fiber_physical_link_decisions_separation",
        ),
        sa.CheckConstraint(
            "(status = 'proposed' AND reviewed_by IS NULL AND reviewed_at IS NULL) OR "
            "(status <> 'proposed' AND reviewed_by IS NOT NULL AND reviewed_at IS NOT NULL)",
            name="ck_fiber_physical_link_decisions_review_evidence",
        ),
        sa.CheckConstraint(
            "(status IN ('applied', 'closed') AND executed_by IS NOT NULL "
            "AND executed_at IS NOT NULL AND result_payload IS NOT NULL "
            "AND result_sha256 IS NOT NULL) OR "
            "(status NOT IN ('applied', 'closed') AND executed_by IS NULL "
            "AND executed_at IS NULL AND result_payload IS NULL "
            "AND result_sha256 IS NULL)",
            name="ck_fiber_physical_link_decisions_result_evidence",
        ),
        sa.CheckConstraint(
            "length(decision_sha256) = 64",
            name="ck_fiber_physical_link_decisions_digest",
        ),
        sa.CheckConstraint(
            "result_sha256 IS NULL OR length(result_sha256) = 64",
            name="ck_fiber_physical_link_decisions_result_digest",
        ),
        sa.ForeignKeyConstraint(
            ["first_strand_id"], ["fiber_strands.id"], ondelete="RESTRICT"
        ),
        sa.ForeignKeyConstraint(
            ["second_strand_id"], ["fiber_strands.id"], ondelete="RESTRICT"
        ),
        sa.ForeignKeyConstraint(
            ["connector_port_id"], ["fiber_connector_ports.id"], ondelete="RESTRICT"
        ),
        sa.ForeignKeyConstraint(
            ["first_connector_port_id"],
            ["fiber_connector_ports.id"],
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["second_connector_port_id"],
            ["fiber_connector_ports.id"],
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["splice_closure_id"],
            ["fiber_splice_closures.id"],
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["splice_tray_id"], ["fiber_splice_trays.id"], ondelete="RESTRICT"
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "decision_sha256", name="uq_fiber_physical_link_decision_sha256"
        ),
    )
    op.create_index(
        "ix_fiber_physical_link_decisions_status",
        "fiber_physical_link_decisions",
        ["status"],
    )

    op.create_table(
        "fiber_core_splices",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("first_strand_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("first_strand_end", sa.String(length=1), nullable=False),
        sa.Column("second_strand_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("second_strand_end", sa.String(length=1), nullable=False),
        sa.Column("splice_closure_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("splice_tray_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("position", sa.Integer(), nullable=True),
        sa.Column("splice_type", sa.String(length=80), nullable=False),
        sa.Column("insertion_loss_db", sa.Numeric(precision=8, scale=3), nullable=True),
        sa.Column(
            "created_by_decision_id", postgresql.UUID(as_uuid=True), nullable=False
        ),
        sa.Column(
            "retired_by_decision_id", postgresql.UUID(as_uuid=True), nullable=True
        ),
        sa.Column("active", sa.Boolean(), nullable=False),
        sa.Column("notes", sa.Text(), nullable=True),
        *_timestamps(),
        sa.CheckConstraint(
            "first_strand_id <> second_strand_id",
            name="ck_fiber_core_splices_distinct_strands",
        ),
        sa.CheckConstraint(
            "first_strand_end IN ('a', 'b') AND second_strand_end IN ('a', 'b')",
            name="ck_fiber_core_splices_ends",
        ),
        sa.CheckConstraint(
            "(active AND retired_by_decision_id IS NULL) OR "
            "(NOT active AND retired_by_decision_id IS NOT NULL)",
            name="ck_fiber_core_splices_retirement",
        ),
        sa.CheckConstraint(
            "position IS NULL OR position > 0",
            name="ck_fiber_core_splices_position",
        ),
        sa.CheckConstraint(
            "insertion_loss_db IS NULL OR insertion_loss_db >= 0",
            name="ck_fiber_core_splices_loss",
        ),
        sa.ForeignKeyConstraint(
            ["first_strand_id"], ["fiber_strands.id"], ondelete="RESTRICT"
        ),
        sa.ForeignKeyConstraint(
            ["second_strand_id"], ["fiber_strands.id"], ondelete="RESTRICT"
        ),
        sa.ForeignKeyConstraint(
            ["splice_closure_id"],
            ["fiber_splice_closures.id"],
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["splice_tray_id"], ["fiber_splice_trays.id"], ondelete="RESTRICT"
        ),
        sa.ForeignKeyConstraint(
            ["created_by_decision_id"],
            ["fiber_physical_link_decisions.id"],
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["retired_by_decision_id"],
            ["fiber_physical_link_decisions.id"],
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "created_by_decision_id", name="uq_fiber_core_splices_create_decision"
        ),
        sa.UniqueConstraint(
            "retired_by_decision_id", name="uq_fiber_core_splices_retire_decision"
        ),
    )
    op.create_index(
        "ix_fiber_core_splices_closure",
        "fiber_core_splices",
        ["splice_closure_id"],
    )

    op.create_table(
        "fiber_strand_terminations",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("strand_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("strand_end", sa.String(length=1), nullable=False),
        sa.Column("connector_port_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column(
            "created_by_decision_id", postgresql.UUID(as_uuid=True), nullable=False
        ),
        sa.Column(
            "retired_by_decision_id", postgresql.UUID(as_uuid=True), nullable=True
        ),
        sa.Column("active", sa.Boolean(), nullable=False),
        sa.Column("notes", sa.Text(), nullable=True),
        *_timestamps(),
        sa.CheckConstraint(
            "strand_end IN ('a', 'b')", name="ck_fiber_strand_terminations_end"
        ),
        sa.CheckConstraint(
            "(active AND retired_by_decision_id IS NULL) OR "
            "(NOT active AND retired_by_decision_id IS NOT NULL)",
            name="ck_fiber_strand_terminations_retirement",
        ),
        sa.ForeignKeyConstraint(
            ["strand_id"], ["fiber_strands.id"], ondelete="RESTRICT"
        ),
        sa.ForeignKeyConstraint(
            ["connector_port_id"], ["fiber_connector_ports.id"], ondelete="RESTRICT"
        ),
        sa.ForeignKeyConstraint(
            ["created_by_decision_id"],
            ["fiber_physical_link_decisions.id"],
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["retired_by_decision_id"],
            ["fiber_physical_link_decisions.id"],
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "created_by_decision_id",
            name="uq_fiber_strand_terminations_create_decision",
        ),
        sa.UniqueConstraint(
            "retired_by_decision_id",
            name="uq_fiber_strand_terminations_retire_decision",
        ),
    )
    op.create_index(
        "uq_fiber_strand_terminations_active_end",
        "fiber_strand_terminations",
        ["strand_id", "strand_end"],
        unique=True,
        postgresql_where=sa.text("active"),
        sqlite_where=sa.text("active"),
    )
    op.create_index(
        "uq_fiber_strand_terminations_active_connector",
        "fiber_strand_terminations",
        ["connector_port_id"],
        unique=True,
        postgresql_where=sa.text("active"),
        sqlite_where=sa.text("active"),
    )

    op.create_table(
        "fiber_patch_cords",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column(
            "first_connector_port_id", postgresql.UUID(as_uuid=True), nullable=False
        ),
        sa.Column(
            "second_connector_port_id", postgresql.UUID(as_uuid=True), nullable=False
        ),
        sa.Column("label", sa.String(length=160), nullable=False),
        sa.Column("assembly_label", sa.String(length=160), nullable=True),
        sa.Column("length_m", sa.Numeric(precision=12, scale=3), nullable=True),
        sa.Column("insertion_loss_db", sa.Numeric(precision=8, scale=3), nullable=True),
        sa.Column(
            "created_by_decision_id", postgresql.UUID(as_uuid=True), nullable=False
        ),
        sa.Column(
            "retired_by_decision_id", postgresql.UUID(as_uuid=True), nullable=True
        ),
        sa.Column("active", sa.Boolean(), nullable=False),
        sa.Column("notes", sa.Text(), nullable=True),
        *_timestamps(),
        sa.CheckConstraint(
            "first_connector_port_id <> second_connector_port_id",
            name="ck_fiber_patch_cords_distinct_connectors",
        ),
        sa.CheckConstraint(
            "(active AND retired_by_decision_id IS NULL) OR "
            "(NOT active AND retired_by_decision_id IS NOT NULL)",
            name="ck_fiber_patch_cords_retirement",
        ),
        sa.CheckConstraint(
            "length_m IS NULL OR length_m > 0", name="ck_fiber_patch_cords_length"
        ),
        sa.CheckConstraint(
            "insertion_loss_db IS NULL OR insertion_loss_db >= 0",
            name="ck_fiber_patch_cords_loss",
        ),
        sa.ForeignKeyConstraint(
            ["first_connector_port_id"],
            ["fiber_connector_ports.id"],
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["second_connector_port_id"],
            ["fiber_connector_ports.id"],
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["created_by_decision_id"],
            ["fiber_physical_link_decisions.id"],
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["retired_by_decision_id"],
            ["fiber_physical_link_decisions.id"],
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "created_by_decision_id", name="uq_fiber_patch_cords_create_decision"
        ),
        sa.UniqueConstraint(
            "retired_by_decision_id", name="uq_fiber_patch_cords_retire_decision"
        ),
    )
    op.create_index(
        "uq_fiber_patch_cords_active_first_connector",
        "fiber_patch_cords",
        ["first_connector_port_id"],
        unique=True,
        postgresql_where=sa.text("active"),
        sqlite_where=sa.text("active"),
    )
    op.create_index(
        "uq_fiber_patch_cords_active_second_connector",
        "fiber_patch_cords",
        ["second_connector_port_id"],
        unique=True,
        postgresql_where=sa.text("active"),
        sqlite_where=sa.text("active"),
    )


def downgrade() -> None:
    op.drop_index(
        "uq_fiber_patch_cords_active_second_connector", table_name="fiber_patch_cords"
    )
    op.drop_index(
        "uq_fiber_patch_cords_active_first_connector", table_name="fiber_patch_cords"
    )
    op.drop_table("fiber_patch_cords")
    op.drop_index(
        "uq_fiber_strand_terminations_active_connector",
        table_name="fiber_strand_terminations",
    )
    op.drop_index(
        "uq_fiber_strand_terminations_active_end",
        table_name="fiber_strand_terminations",
    )
    op.drop_table("fiber_strand_terminations")
    op.drop_index("ix_fiber_core_splices_closure", table_name="fiber_core_splices")
    op.drop_table("fiber_core_splices")
    op.drop_index(
        "ix_fiber_physical_link_decisions_status",
        table_name="fiber_physical_link_decisions",
    )
    op.drop_table("fiber_physical_link_decisions")
    op.drop_table("fiber_connector_ports")
    op.drop_table("fiber_patch_panels")
    op.drop_table("fiber_racks")
