"""Add genieacs to provisioning vendor enum.

Revision ID: 6f1c2d3e4b5a
Revises: 2883b6622e2d
Create Date: 2026-02-01
"""

from alembic import op

revision = "6f1c2d3e4b5a"
down_revision = "2883b6622e2d"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("ALTER TYPE provisioningvendor ADD VALUE IF NOT EXISTS 'genieacs'")


def downgrade() -> None:
    # Enum value removal is not supported in PostgreSQL without recreating the type.
    pass
