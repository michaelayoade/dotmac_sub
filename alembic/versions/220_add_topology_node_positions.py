"""Add saved topology node positions.

Revision ID: 220_add_topology_node_positions
Revises: 219_add_drift_finding_evidence
Create Date: 2026-07-07
"""

import sqlalchemy as sa
from sqlalchemy import inspect

from alembic import op

revision = "220_add_topology_node_positions"
down_revision = "219_add_drift_finding_evidence"
branch_labels = None
depends_on = None

_TABLE = "network_devices"


def _has_column(inspector, table: str, column: str) -> bool:
    if table not in inspector.get_table_names():
        return False
    return any(c["name"] == column for c in inspector.get_columns(table))


def upgrade() -> None:
    bind = op.get_bind()
    inspector = inspect(bind)
    if _TABLE not in inspector.get_table_names():
        return
    if not _has_column(inspector, _TABLE, "topology_x"):
        op.add_column(_TABLE, sa.Column("topology_x", sa.Float(), nullable=True))
    if not _has_column(inspector, _TABLE, "topology_y"):
        op.add_column(_TABLE, sa.Column("topology_y", sa.Float(), nullable=True))


def downgrade() -> None:
    bind = op.get_bind()
    inspector = inspect(bind)
    if _has_column(inspector, _TABLE, "topology_y"):
        op.drop_column(_TABLE, "topology_y")
    if _has_column(inspector, _TABLE, "topology_x"):
        op.drop_column(_TABLE, "topology_x")
