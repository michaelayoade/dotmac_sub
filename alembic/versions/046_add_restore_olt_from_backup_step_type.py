"""Add restore_olt_from_backup provisioning step type.

Revision ID: 046_add_restore_olt_from_backup_step_type
Revises: 044_add_olt_observed_snapshots, 045_contact_channels_without_name
Create Date: 2026-04-21
"""

from __future__ import annotations

from alembic import op

revision = "046_add_restore_olt_from_backup_step_type"
down_revision = ("044_add_olt_observed_snapshots", "045_contact_channels_without_name")
branch_labels = None
depends_on = None

_NEW_VALUE = "restore_olt_from_backup"


def upgrade() -> None:
    op.execute(
        f"ALTER TYPE provisioningsteptype ADD VALUE IF NOT EXISTS '{_NEW_VALUE}'"
    )


def downgrade() -> None:
    # PostgreSQL does not support removing enum values from an existing type.
    pass
