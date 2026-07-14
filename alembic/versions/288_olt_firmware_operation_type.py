"""Add tracked OLT firmware upgrade operations.

Revision ID: 288_olt_firmware_operation_type
Revises: 287_ont_composite_config_evidence
"""

from __future__ import annotations

from alembic import op

revision = "288_olt_firmware_operation_type"
down_revision = "287_ont_composite_config_evidence"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name == "postgresql":
        with op.get_context().autocommit_block():
            op.execute(
                "ALTER TYPE networkoperationtype "
                "ADD VALUE IF NOT EXISTS 'olt_firmware_upgrade'"
            )


def downgrade() -> None:
    # PostgreSQL enum values cannot be safely removed while operation history
    # may reference them.
    pass
