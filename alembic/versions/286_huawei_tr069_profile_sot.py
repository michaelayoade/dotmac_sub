"""Persist desired and observed Huawei TR-069 OLT profiles.

Revision ID: 286_huawei_tr069_profile_sot
Revises: 285_ont_lifecycle_operation_types
"""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision = "286_huawei_tr069_profile_sot"
down_revision = "285_ont_lifecycle_operation_types"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "ont_units",
        sa.Column("desired_tr069_profile_id", sa.Integer(), nullable=True),
    )
    op.add_column(
        "ont_observations",
        sa.Column("olt_tr069_profile_id", sa.Integer(), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("ont_observations", "olt_tr069_profile_id")
    op.drop_column("ont_units", "desired_tr069_profile_id")
