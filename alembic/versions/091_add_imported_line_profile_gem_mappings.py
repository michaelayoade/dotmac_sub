"""Add imported OLT line profile GEM mappings.

Revision ID: 091_add_imported_line_profile_gem_mappings
Revises: 090_ensure_olt_capabilities_source
Create Date: 2026-05-06
"""

from __future__ import annotations

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision = "091_add_imported_line_profile_gem_mappings"
down_revision = "090_ensure_olt_capabilities_source"
branch_labels = None
depends_on = None


def _table_exists(table_name: str) -> bool:
    inspector = sa.inspect(op.get_bind())
    return table_name in inspector.get_table_names()


def upgrade() -> None:
    if _table_exists("olt_line_profile_gem_mappings"):
        return
    op.create_table(
        "olt_line_profile_gem_mappings",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("olt_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("line_profile_id", sa.Integer(), nullable=False),
        sa.Column("source", sa.String(length=40), nullable=False),
        sa.Column("source_key", sa.String(length=160), nullable=False),
        sa.Column("gem_index", sa.Integer(), nullable=False),
        sa.Column("mapping_index", sa.Integer(), nullable=True),
        sa.Column("tcont_index", sa.Integer(), nullable=True),
        sa.Column("vlan_id", sa.Integer(), nullable=True),
        sa.Column("priority", sa.Integer(), nullable=True),
        sa.Column("eth_port", sa.Integer(), nullable=True),
        sa.Column("usage_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("raw_config", sa.Text(), nullable=True),
        sa.Column("last_imported_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["olt_id"],
            ["olt_devices.id"],
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["olt_id", "line_profile_id"],
            ["olt_line_profiles.olt_id", "olt_line_profiles.profile_id"],
            name="fk_olt_line_profile_gem_mapping_line_profile",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "olt_id",
            "line_profile_id",
            "source_key",
            name="uq_olt_line_profile_gem_mappings_source",
        ),
    )


def downgrade() -> None:
    if _table_exists("olt_line_profile_gem_mappings"):
        op.drop_table("olt_line_profile_gem_mappings")
