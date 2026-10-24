"""Persist readback drift field names on ONT service configuration revisions.

Adds one nullable JSON column holding field names only (never values).
Additive; no backfill.

Revision ID: 700_ont_service_config_drift_fields
Revises: 699_dispatch_reconciliation_resolution
Create Date: 2026-10-24
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "700_ont_service_config_drift_fields"
down_revision: str | None = "699_dispatch_reconciliation_resolution"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_TABLE = "ont_service_configuration_revisions"


def upgrade() -> None:
    bind = op.get_bind()
    columns = {c["name"] for c in sa.inspect(bind).get_columns(_TABLE)}
    if "drift_fields" not in columns:
        op.add_column(_TABLE, sa.Column("drift_fields", sa.JSON(), nullable=True))


def downgrade() -> None:
    bind = op.get_bind()
    columns = {c["name"] for c in sa.inspect(bind).get_columns(_TABLE)}
    if "drift_fields" in columns:
        op.drop_column(_TABLE, "drift_fields")
