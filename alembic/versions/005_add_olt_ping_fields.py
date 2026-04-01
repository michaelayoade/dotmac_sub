"""Add OLT ping reachability fields.

Adds fields to track network-level reachability via ping:
- last_ping_at: When the OLT was last pinged
- last_ping_ok: Whether the ping succeeded

Revision ID: 005_add_olt_ping_fields
Revises: 004_add_olt_polling_health_fields
Create Date: 2026-04-01

"""

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision = "005_add_olt_ping_fields"
down_revision = "003_add_olt_polling_health_fields"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "olt_devices",
        sa.Column("last_ping_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.add_column(
        "olt_devices",
        sa.Column("last_ping_ok", sa.Boolean(), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("olt_devices", "last_ping_ok")
    op.drop_column("olt_devices", "last_ping_at")
