"""Add released_at and release_reason to ont_assignments

Revision ID: 057_add_ont_assignment_release_fields
Revises: 056_drop_periodic_inform_server_default
Create Date: 2026-04-24

"""

from alembic import op
import sqlalchemy as sa

revision = "057_add_ont_assignment_release_fields"
down_revision = "056_drop_inform_server_default"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Add released_at and release_reason columns to ont_assignments for audit trail
    # These track when an assignment was closed and why (e.g., "returned_to_inventory")
    op.add_column(
        "ont_assignments",
        sa.Column("released_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.add_column(
        "ont_assignments",
        sa.Column("release_reason", sa.String(64), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("ont_assignments", "release_reason")
    op.drop_column("ont_assignments", "released_at")
