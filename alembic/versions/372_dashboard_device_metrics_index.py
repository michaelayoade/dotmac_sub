"""Index the dashboard's recent receive-bandwidth aggregate.

Revision ID: 372_dashboard_device_metrics_index
Revises: 371_retire_coarse_reports_permissions
"""

from __future__ import annotations

from alembic import op

revision = "372_dashboard_device_metrics_index"
down_revision = "371_retire_coarse_reports_permissions"
branch_labels = None
depends_on = None

_INDEX = "ix_device_metrics_rx_bps_recorded_at"


def upgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name == "postgresql":
        with op.get_context().autocommit_block():
            op.execute(
                f"CREATE INDEX CONCURRENTLY IF NOT EXISTS {_INDEX} "
                "ON device_metrics (recorded_at) INCLUDE (value) "
                "WHERE metric_type = 'rx_bps' AND value > 0"
            )
    else:
        op.execute(
            f"CREATE INDEX IF NOT EXISTS {_INDEX} "
            "ON device_metrics (recorded_at) "
            "WHERE metric_type = 'rx_bps' AND value > 0"
        )


def downgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name == "postgresql":
        with op.get_context().autocommit_block():
            op.execute(f"DROP INDEX CONCURRENTLY IF EXISTS {_INDEX}")
    else:
        op.execute(f"DROP INDEX IF EXISTS {_INDEX}")
