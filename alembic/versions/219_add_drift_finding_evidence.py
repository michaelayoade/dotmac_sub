"""Add evidence JSON to cross_app_drift_findings.

A compact both-sides-of-the-mismatch snapshot (e.g. billing_status vs
radius_authorized + active_sessions) kept distinct from ``details`` (which
carries the remediation: suggested_owner / suggested_action). Makes dashboard
and incident review straightforward.

Revision ID: 219_add_drift_finding_evidence
Revises: 218_add_cross_app_drift_tables
Create Date: 2026-07-07
"""

import sqlalchemy as sa
from sqlalchemy import inspect

from alembic import op

revision = "219_add_drift_finding_evidence"
down_revision = "218_add_cross_app_drift_tables"
branch_labels = None
depends_on = None

_TABLE = "cross_app_drift_findings"
_COLUMN = "evidence"


def _has_column(inspector, table: str, column: str) -> bool:
    if table not in inspector.get_table_names():
        return False
    return any(c["name"] == column for c in inspector.get_columns(table))


def upgrade() -> None:
    bind = op.get_bind()
    inspector = inspect(bind)
    if _TABLE not in inspector.get_table_names() or _has_column(
        inspector, _TABLE, _COLUMN
    ):
        return
    op.add_column(_TABLE, sa.Column(_COLUMN, sa.JSON()))


def downgrade() -> None:
    bind = op.get_bind()
    inspector = inspect(bind)
    if _has_column(inspector, _TABLE, _COLUMN):
        op.drop_column(_TABLE, _COLUMN)
