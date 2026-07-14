"""Add tracked ONT lifecycle operation types.

Revision ID: 285_ont_lifecycle_operation_types
Revises: 284_ont_firmware_operation_type
"""

from __future__ import annotations

from alembic import op

revision = "285_ont_lifecycle_operation_types"
down_revision = "284_ont_firmware_operation_type"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name == "postgresql":
        with op.get_context().autocommit_block():
            op.execute(
                "ALTER TYPE networkoperationtype "
                "ADD VALUE IF NOT EXISTS 'ont_return_to_inventory'"
            )
            op.execute(
                "ALTER TYPE networkoperationtype "
                "ADD VALUE IF NOT EXISTS 'ont_decommission'"
            )


def downgrade() -> None:
    # PostgreSQL enum values cannot be safely removed while operation history
    # may reference them.
    pass
