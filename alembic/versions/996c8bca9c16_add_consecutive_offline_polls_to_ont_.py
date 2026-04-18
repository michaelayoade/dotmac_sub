"""add_consecutive_offline_polls_to_ont_units

Revision ID: 996c8bca9c16
Revises: ffdddc71211e
Create Date: 2026-04-02 16:36:24.209901

"""

import sqlalchemy as sa

from alembic import op

revision = "996c8bca9c16"
down_revision = "ffdddc71211e"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "ont_units",
        sa.Column(
            "consecutive_offline_polls",
            sa.Integer(),
            nullable=False,
            server_default="0",
        ),
    )


def downgrade() -> None:
    op.drop_column("ont_units", "consecutive_offline_polls")
