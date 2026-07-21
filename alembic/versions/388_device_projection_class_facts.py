"""device_projection class_facts column

Adds a nullable JSONB column holding per-class operational facts (ONT signal,
OLT PON rollup, core site/role, NAS health, router RouterOS version) that the
reconciler denormalises for the unified Device ledger/360. device_projections is
a rebuildable cache, so this is additive and back-filled on the next reconcile.

Revision ID: 388_device_projection_class_facts
Revises: 387_dashboard_device_metrics_index
"""

from __future__ import annotations

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision = "388_device_projection_class_facts"
down_revision = "387_dashboard_device_metrics_index"
branch_labels = None
depends_on = None


def upgrade() -> None:
    columns = {
        item["name"]
        for item in sa.inspect(op.get_bind()).get_columns("device_projections")
    }
    if "class_facts" not in columns:
        op.add_column(
            "device_projections",
            sa.Column(
                "class_facts", postgresql.JSONB(astext_type=sa.Text()), nullable=True
            ),
        )


def downgrade() -> None:
    columns = {
        item["name"]
        for item in sa.inspect(op.get_bind()).get_columns("device_projections")
    }
    if "class_facts" in columns:
        op.drop_column("device_projections", "class_facts")
