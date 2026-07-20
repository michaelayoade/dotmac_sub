"""Add tracked ONT firmware upgrade operations.

Revision ID: 284_ont_firmware_operation_type
Revises: 283_huawei_remote_access_observation
"""

from __future__ import annotations

from alembic import op

revision = "284_ont_firmware_operation_type"
down_revision = "283_huawei_remote_access_observation"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name == "postgresql":
        with op.get_context().autocommit_block():
            op.execute(
                "ALTER TYPE networkoperationtype "
                "ADD VALUE IF NOT EXISTS 'ont_firmware_upgrade'"
            )


def downgrade() -> None:
    # PostgreSQL enum values cannot be safely removed while operation history
    # may reference them.
    pass
