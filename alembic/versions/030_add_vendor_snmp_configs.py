"""Add vendor_snmp_configs table for per-vendor SNMP configuration.

Allows customization of SNMP walk strategy, timeouts, and OID overrides
on a per-vendor or per-model basis. Addresses issue #17 where bulkwalk
strategy was hardcoded.

Revision ID: 030_add_vendor_snmp_configs
Revises: 029_add_task_executions
Create Date: 2026-04-17
"""

from __future__ import annotations

import sqlalchemy as sa
from sqlalchemy import inspect
from sqlalchemy.dialects import postgresql

from alembic import op

revision = "030_add_vendor_snmp_configs"
down_revision = "029_add_task_executions"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    inspector = inspect(bind)

    # Check if table already exists (idempotent)
    if "vendor_snmp_configs" in inspector.get_table_names():
        return

    op.create_table(
        "vendor_snmp_configs",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("vendor", sa.String(120), nullable=False),
        sa.Column("model", sa.String(120), nullable=True),
        sa.Column("walk_strategy", sa.String(20), server_default="single"),
        sa.Column("walk_timeout_seconds", sa.Integer, server_default="90"),
        sa.Column("walk_max_repetitions", sa.Integer, server_default="50"),
        sa.Column("oid_overrides", postgresql.JSON, nullable=True),
        sa.Column("signal_scale", sa.Float, nullable=True),
        sa.Column("priority", sa.Integer, server_default="0"),
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

    op.create_unique_constraint(
        "uq_vendor_snmp_config",
        "vendor_snmp_configs",
        ["vendor", "model"],
    )

    # Seed default config for Huawei MA5608T (known to have bulkwalk issues)
    op.execute("""
        INSERT INTO vendor_snmp_configs (id, vendor, model, walk_strategy, notes)
        VALUES (
            gen_random_uuid(),
            'Huawei',
            'MA5608T',
            'single',
            'MA5608T has bulkwalk timeout issues on certain OID ranges'
        )
        ON CONFLICT DO NOTHING
    """)


def downgrade() -> None:
    bind = op.get_bind()
    inspector = inspect(bind)

    if "vendor_snmp_configs" not in inspector.get_table_names():
        return

    op.drop_table("vendor_snmp_configs")
