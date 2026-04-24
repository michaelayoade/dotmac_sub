"""Add autofind_last_sync_at to olt_devices for deduplication

Revision ID: 058_add_olt_autofind_last_sync_at
Revises: 057_add_ont_assignment_release_fields
Create Date: 2026-04-24

"""

from alembic import op
import sqlalchemy as sa

revision = "058_add_olt_autofind_last_sync_at"
down_revision = "057_add_ont_assignment_release_fields"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Add autofind_last_sync_at to track when autofind was last refreshed
    # This prevents redundant SSH queries during concurrent authorizations
    op.add_column(
        "olt_devices",
        sa.Column("autofind_last_sync_at", sa.DateTime(timezone=True), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("olt_devices", "autofind_last_sync_at")
