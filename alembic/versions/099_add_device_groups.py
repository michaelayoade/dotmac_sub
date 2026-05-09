"""Add network device groups.

Revision ID: 099_add_device_groups
Revises: 098_add_olt_profile_bundles
Create Date: 2026-05-09
"""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision = "099_add_device_groups"
down_revision = "098_add_olt_profile_bundles"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "device_groups",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("name", sa.String(length=120), nullable=False),
        sa.Column("kind", sa.String(length=40), nullable=False),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column("criteria", sa.JSON(), nullable=True),
        sa.Column("created_by", sa.String(length=120), nullable=True),
        sa.Column("is_active", sa.Boolean(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("name", name="uq_device_groups_name"),
    )
    op.create_index(
        "ix_device_groups_kind_active",
        "device_groups",
        ["kind", "is_active"],
    )
    op.create_table(
        "device_group_members",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("group_id", sa.UUID(), nullable=False),
        sa.Column("device_type", sa.String(length=40), nullable=False),
        sa.Column("device_id", sa.UUID(), nullable=False),
        sa.Column("added_by", sa.String(length=120), nullable=True),
        sa.Column("added_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("metadata_json", sa.JSON(), nullable=True),
        sa.ForeignKeyConstraint(["group_id"], ["device_groups.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "group_id",
            "device_type",
            "device_id",
            name="uq_device_group_members_group_device",
        ),
    )
    op.create_index(
        "ix_device_group_members_group_type",
        "device_group_members",
        ["group_id", "device_type"],
    )


def downgrade() -> None:
    op.drop_index(
        "ix_device_group_members_group_type",
        table_name="device_group_members",
    )
    op.drop_table("device_group_members")
    op.drop_index("ix_device_groups_kind_active", table_name="device_groups")
    op.drop_table("device_groups")
