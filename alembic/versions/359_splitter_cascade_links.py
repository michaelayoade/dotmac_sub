"""Add reviewed exact splitter cascade links.

Revision ID: 359_splitter_cascade_links
Revises: 358_fiber_support_structures
Create Date: 2026-07-18
"""

from __future__ import annotations

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision = "359_splitter_cascade_links"
down_revision = "358_fiber_support_structures"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "splitters",
        sa.Column("insertion_loss_db", sa.Numeric(8, 3), nullable=True),
    )
    op.create_check_constraint(
        "ck_splitters_insertion_loss_db",
        "splitters",
        "insertion_loss_db IS NULL OR "
        "(insertion_loss_db >= 0 AND insertion_loss_db <= 100)",
    )

    op.add_column(
        "fiber_access_attachment_decisions",
        sa.Column(
            "upstream_splitter_id",
            postgresql.UUID(as_uuid=True),
            nullable=True,
        ),
    )
    op.add_column(
        "fiber_access_attachment_decisions",
        sa.Column("splitter_stage", sa.Integer(), nullable=True),
    )
    op.add_column(
        "fiber_access_attachment_decisions",
        sa.Column("cumulative_loss_db", sa.Numeric(10, 3), nullable=True),
    )
    op.create_foreign_key(
        "fk_fiber_access_attachment_upstream_splitter",
        "fiber_access_attachment_decisions",
        "splitters",
        ["upstream_splitter_id"],
        ["id"],
        ondelete="RESTRICT",
    )
    op.drop_constraint(
        "ck_fiber_access_attachment_type",
        "fiber_access_attachment_decisions",
        type_="check",
    )
    op.create_check_constraint(
        "ck_fiber_access_attachment_type",
        "fiber_access_attachment_decisions",
        "attachment_type IN ('pon_input', 'ont_output', 'splitter_cascade')",
    )
    op.create_check_constraint(
        "ck_fiber_access_attachment_cascade_evidence",
        "fiber_access_attachment_decisions",
        "(attachment_type = 'splitter_cascade' "
        "AND upstream_splitter_id IS NOT NULL "
        "AND splitter_stage IS NOT NULL AND splitter_stage >= 2 "
        "AND cumulative_loss_db IS NOT NULL AND cumulative_loss_db >= 0) OR "
        "(attachment_type <> 'splitter_cascade' "
        "AND upstream_splitter_id IS NULL "
        "AND splitter_stage IS NULL AND cumulative_loss_db IS NULL)",
    )
    op.create_index(
        "uq_fiber_access_attachment_active_target",
        "fiber_access_attachment_decisions",
        ["target_splitter_port_id"],
        unique=True,
        postgresql_where=sa.text(
            "status IN ('proposed', 'approved') AND target_splitter_port_id IS NOT NULL"
        ),
        sqlite_where=sa.text(
            "status IN ('proposed', 'approved') AND target_splitter_port_id IS NOT NULL"
        ),
    )

    op.create_table(
        "splitter_cascade_links",
        sa.Column(
            "id", postgresql.UUID(as_uuid=True), nullable=False, primary_key=True
        ),
        sa.Column(
            "upstream_output_port_id",
            postgresql.UUID(as_uuid=True),
            nullable=False,
        ),
        sa.Column(
            "downstream_input_port_id",
            postgresql.UUID(as_uuid=True),
            nullable=False,
        ),
        sa.Column(
            "created_by_decision_id",
            postgresql.UUID(as_uuid=True),
            nullable=False,
        ),
        sa.Column(
            "retired_by_decision_id",
            postgresql.UUID(as_uuid=True),
            nullable=True,
        ),
        sa.Column("active", sa.Boolean(), nullable=False),
        sa.Column("notes", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "upstream_output_port_id <> downstream_input_port_id",
            name="ck_splitter_cascade_links_distinct_ports",
        ),
        sa.CheckConstraint(
            "(active AND retired_by_decision_id IS NULL) OR "
            "(NOT active AND retired_by_decision_id IS NOT NULL)",
            name="ck_splitter_cascade_links_retirement",
        ),
        sa.ForeignKeyConstraint(
            ["upstream_output_port_id"],
            ["splitter_ports.id"],
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["downstream_input_port_id"],
            ["splitter_ports.id"],
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["created_by_decision_id"],
            ["fiber_access_attachment_decisions.id"],
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["retired_by_decision_id"],
            ["fiber_access_attachment_decisions.id"],
            ondelete="RESTRICT",
        ),
        sa.UniqueConstraint(
            "created_by_decision_id",
            name="uq_splitter_cascade_links_create_decision",
        ),
        sa.UniqueConstraint(
            "retired_by_decision_id",
            name="uq_splitter_cascade_links_retire_decision",
        ),
    )
    op.create_index(
        "uq_splitter_cascade_links_active_output",
        "splitter_cascade_links",
        ["upstream_output_port_id"],
        unique=True,
        postgresql_where=sa.text("active"),
        sqlite_where=sa.text("active = 1"),
    )
    op.create_index(
        "uq_splitter_cascade_links_active_input",
        "splitter_cascade_links",
        ["downstream_input_port_id"],
        unique=True,
        postgresql_where=sa.text("active"),
        sqlite_where=sa.text("active = 1"),
    )
    op.create_index(
        "ix_splitter_cascade_links_downstream_input",
        "splitter_cascade_links",
        ["downstream_input_port_id"],
    )


def downgrade() -> None:
    op.drop_index(
        "ix_splitter_cascade_links_downstream_input",
        table_name="splitter_cascade_links",
    )
    op.drop_index(
        "uq_splitter_cascade_links_active_input",
        table_name="splitter_cascade_links",
    )
    op.drop_index(
        "uq_splitter_cascade_links_active_output",
        table_name="splitter_cascade_links",
    )
    op.drop_table("splitter_cascade_links")

    op.drop_index(
        "uq_fiber_access_attachment_active_target",
        table_name="fiber_access_attachment_decisions",
    )
    op.drop_constraint(
        "ck_fiber_access_attachment_cascade_evidence",
        "fiber_access_attachment_decisions",
        type_="check",
    )
    op.drop_constraint(
        "ck_fiber_access_attachment_type",
        "fiber_access_attachment_decisions",
        type_="check",
    )
    op.create_check_constraint(
        "ck_fiber_access_attachment_type",
        "fiber_access_attachment_decisions",
        "attachment_type IN ('pon_input', 'ont_output')",
    )
    op.drop_constraint(
        "fk_fiber_access_attachment_upstream_splitter",
        "fiber_access_attachment_decisions",
        type_="foreignkey",
    )
    op.drop_column("fiber_access_attachment_decisions", "cumulative_loss_db")
    op.drop_column("fiber_access_attachment_decisions", "splitter_stage")
    op.drop_column("fiber_access_attachment_decisions", "upstream_splitter_id")

    op.drop_constraint("ck_splitters_insertion_loss_db", "splitters", type_="check")
    op.drop_column("splitters", "insertion_loss_db")
