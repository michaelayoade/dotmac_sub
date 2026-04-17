"""Add signal_threshold_overrides table for per-OLT/model thresholds.

Allows customization of warning/critical signal thresholds on a per-OLT
or per-model basis. Addresses issue #18 where thresholds were global only.

Revision ID: 031_add_signal_threshold_overrides
Revises: 030_add_vendor_snmp_configs
Create Date: 2026-04-17
"""

from __future__ import annotations

import sqlalchemy as sa
from sqlalchemy import inspect
from sqlalchemy.dialects import postgresql

from alembic import op

revision = "031_add_signal_threshold_overrides"
down_revision = "030_add_vendor_snmp_configs"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    inspector = inspect(bind)

    # Check if table already exists (idempotent)
    if "signal_threshold_overrides" in inspector.get_table_names():
        return

    op.create_table(
        "signal_threshold_overrides",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "olt_device_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("olt_devices.id", ondelete="CASCADE"),
            nullable=True,
        ),
        sa.Column("model_pattern", sa.String(120), nullable=True),
        sa.Column("warning_threshold_dbm", sa.Float, nullable=True),
        sa.Column("critical_threshold_dbm", sa.Float, nullable=True),
        sa.Column("is_active", sa.Boolean, server_default="true"),
        sa.Column("notes", sa.Text, nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
        ),
    )

    # Check constraint: either OLT or model pattern, not both
    op.create_check_constraint(
        "ck_threshold_override_scope",
        "signal_threshold_overrides",
        "NOT (olt_device_id IS NOT NULL AND model_pattern IS NOT NULL)",
    )

    # Index for efficient lookup by OLT
    op.create_index(
        "ix_signal_threshold_overrides_olt_device_id",
        "signal_threshold_overrides",
        ["olt_device_id"],
    )


def downgrade() -> None:
    bind = op.get_bind()
    inspector = inspect(bind)

    if "signal_threshold_overrides" not in inspector.get_table_names():
        return

    op.drop_index(
        "ix_signal_threshold_overrides_olt_device_id",
        table_name="signal_threshold_overrides",
    )
    op.drop_table("signal_threshold_overrides")
