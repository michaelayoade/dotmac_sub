"""add olt autofind candidates

Revision ID: c3d4e5f6a7b9
Revises: b4f8c2a71e53
Create Date: 2026-03-23 12:30:00.000000
"""

from __future__ import annotations

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

# revision identifiers, used by Alembic.
revision = "c3d4e5f6a7b9"
down_revision = "b4f8c2a71e53"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "olt_autofind_candidates",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("olt_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("ont_unit_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("fsp", sa.String(length=32), nullable=False),
        sa.Column("serial_number", sa.String(length=120), nullable=False),
        sa.Column("serial_hex", sa.String(length=32), nullable=True),
        sa.Column("vendor_id", sa.String(length=32), nullable=True),
        sa.Column("model", sa.String(length=120), nullable=True),
        sa.Column("software_version", sa.String(length=160), nullable=True),
        sa.Column("mac", sa.String(length=32), nullable=True),
        sa.Column("equipment_sn", sa.String(length=120), nullable=True),
        sa.Column("autofind_time", sa.String(length=120), nullable=True),
        sa.Column(
            "is_active", sa.Boolean(), nullable=False, server_default=sa.text("true")
        ),
        sa.Column("resolution_reason", sa.String(length=64), nullable=True),
        sa.Column("notes", sa.Text(), nullable=True),
        sa.Column(
            "first_seen_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.Column(
            "last_seen_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.Column("resolved_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.ForeignKeyConstraint(["olt_id"], ["olt_devices.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["ont_unit_id"], ["ont_units.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "olt_id",
            "fsp",
            "serial_number",
            name="uq_olt_autofind_candidates_olt_fsp_serial",
        ),
    )
    op.create_index(
        "ix_olt_autofind_candidates_active",
        "olt_autofind_candidates",
        ["is_active"],
        unique=False,
    )
    op.create_index(
        "ix_olt_autofind_candidates_olt_active",
        "olt_autofind_candidates",
        ["olt_id", "is_active"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index(
        "ix_olt_autofind_candidates_olt_active", table_name="olt_autofind_candidates"
    )
    op.drop_index(
        "ix_olt_autofind_candidates_active", table_name="olt_autofind_candidates"
    )
    op.drop_table("olt_autofind_candidates")
