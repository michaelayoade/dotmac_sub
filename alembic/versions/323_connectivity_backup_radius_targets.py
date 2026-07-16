"""Store multi-target external RADIUS connectivity snapshots.

Revision ID: 323_connectivity_backup_radius_targets
Revises: 322_ledger_customer_position_effect
"""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision = "323_connectivity_backup_radius_targets"
down_revision = "322_ledger_customer_position_effect"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "connectivity_state_backups",
        sa.Column("radius_targets", sa.JSON(), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("connectivity_state_backups", "radius_targets")
