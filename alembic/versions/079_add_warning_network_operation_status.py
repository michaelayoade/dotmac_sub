"""Add warning network operation status.

Revision ID: 079_add_warning_network_operation_status
Revises: 078_clean_ont_status_model
Create Date: 2026-04-29
"""

from __future__ import annotations

from alembic import op

revision = "079_add_warning_network_operation_status"
down_revision = "078_clean_ont_status_model"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name == "postgresql":
        with op.get_context().autocommit_block():
            op.execute(
                "ALTER TYPE networkoperationstatus ADD VALUE IF NOT EXISTS 'warning'"
            )


def downgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name == "postgresql":
        op.execute(
            "UPDATE network_operations SET status = 'failed' WHERE status = 'warning'"
        )
        # PostgreSQL cannot drop enum values without recreating the type. Keep the
        # value in place after data downgrade to avoid unsafe type surgery.
