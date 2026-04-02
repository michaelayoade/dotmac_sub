"""add ont tr069 snapshot cache

Revision ID: 010_add_ont_tr069_snapshot_cache
Revises: 009_enforce_single_active_tr069_link_per_ont
Create Date: 2026-04-02
"""

import sqlalchemy as sa

from alembic import op

revision = "010_add_ont_tr069_snapshot_cache"
down_revision = "009_enforce_single_active_tr069_link_per_ont"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "ont_units",
        sa.Column("tr069_last_snapshot", sa.JSON(), nullable=True),
    )
    op.add_column(
        "ont_units",
        sa.Column("tr069_last_snapshot_at", sa.DateTime(timezone=True), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("ont_units", "tr069_last_snapshot_at")
    op.drop_column("ont_units", "tr069_last_snapshot")
