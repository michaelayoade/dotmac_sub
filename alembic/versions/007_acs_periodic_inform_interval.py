"""Add periodic_inform_interval to tr069_acs_servers.

Revision ID: 007_acs_periodic_inform_interval
Revises: 006_ont_external_id_unique
Create Date: 2026-04-01
"""

from alembic import op
import sqlalchemy as sa

revision = "007_acs_periodic_inform_interval"
down_revision = "006_ont_external_id_unique"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Add periodic_inform_interval column with default 300 seconds (5 minutes)
    op.add_column(
        "tr069_acs_servers",
        sa.Column(
            "periodic_inform_interval",
            sa.Integer(),
            nullable=False,
            server_default="300",
        ),
    )


def downgrade() -> None:
    op.drop_column("tr069_acs_servers", "periodic_inform_interval")
