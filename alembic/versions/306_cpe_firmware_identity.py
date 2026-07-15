"""Persist observed CPE firmware for version-aware adapter resolution.

Revision ID: 293_cpe_firmware_identity
Revises: 292_merge_lifecycle_schedules_and_tax_point_heads
"""

import sqlalchemy as sa

from alembic import op

revision = "306_cpe_firmware_identity"
down_revision = "305_consolidated_payment_settlement"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "cpe_devices",
        sa.Column("firmware_version", sa.String(length=120), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("cpe_devices", "firmware_version")
