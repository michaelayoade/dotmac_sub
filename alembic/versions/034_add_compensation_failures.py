"""add compensation_failures table

Revision ID: 034_add_compensation_failures
Revises: 85f2cdc1eedd
Create Date: 2026-04-18

Tracks failed compensation (rollback) entries for manual resolution.
"""

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision = "034_add_compensation_failures"
down_revision = "85f2cdc1eedd"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Check if table already exists (idempotent)
    conn = op.get_bind()
    inspector = sa.inspect(conn)
    if "compensation_failures" in inspector.get_table_names():
        return

    # Create enum type if it doesn't exist
    compensationstatus = postgresql.ENUM(
        "pending", "resolved", "abandoned", name="compensationstatus", create_type=False
    )
    compensationstatus.create(conn, checkfirst=True)

    op.create_table(
        "compensation_failures",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("ont_unit_id", sa.UUID(), nullable=True),
        sa.Column("olt_device_id", sa.UUID(), nullable=True),
        sa.Column("operation_type", sa.String(length=64), nullable=False),
        sa.Column("step_name", sa.String(length=128), nullable=False),
        sa.Column(
            "undo_commands", postgresql.JSON(astext_type=sa.Text()), nullable=False
        ),
        sa.Column("description", sa.Text(), nullable=False),
        sa.Column("resource_id", sa.String(length=128), nullable=True),
        sa.Column("interface_path", sa.String(length=64), nullable=True),
        sa.Column("error_message", sa.Text(), nullable=False),
        sa.Column("failure_count", sa.Integer(), nullable=False, server_default="1"),
        sa.Column(
            "last_attempted_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.Column(
            "status",
            compensationstatus,
            nullable=False,
            server_default="pending",
        ),
        sa.Column("resolved_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("resolved_by", sa.String(length=128), nullable=True),
        sa.Column("resolution_notes", sa.Text(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.ForeignKeyConstraint(
            ["ont_unit_id"],
            ["ont_units.id"],
        ),
        sa.ForeignKeyConstraint(
            ["olt_device_id"],
            ["olt_devices.id"],
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_compensation_failures_ont_unit", "compensation_failures", ["ont_unit_id"]
    )
    op.create_index(
        "ix_compensation_failures_olt_device",
        "compensation_failures",
        ["olt_device_id"],
    )
    op.create_index(
        "ix_compensation_failures_status", "compensation_failures", ["status"]
    )
    op.create_index(
        "ix_compensation_failures_created_at", "compensation_failures", ["created_at"]
    )


def downgrade() -> None:
    op.drop_index(
        "ix_compensation_failures_created_at", table_name="compensation_failures"
    )
    op.drop_index("ix_compensation_failures_status", table_name="compensation_failures")
    op.drop_index(
        "ix_compensation_failures_olt_device", table_name="compensation_failures"
    )
    op.drop_index(
        "ix_compensation_failures_ont_unit", table_name="compensation_failures"
    )
    op.drop_table("compensation_failures")

    # Note: We don't drop the enum type as it may be used by other code
