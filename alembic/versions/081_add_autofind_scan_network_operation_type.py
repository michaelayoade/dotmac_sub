"""Add autofind scan network operation type.

Revision ID: 081_add_autofind_scan_network_operation_type
Revises: 080_add_ont_management_ip_tracking
Create Date: 2026-04-30
"""

from __future__ import annotations

from alembic import op

revision = "081_add_autofind_scan_network_operation_type"
down_revision = "080_add_ont_management_ip_tracking"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name == "postgresql":
        with op.get_context().autocommit_block():
            op.execute(
                "ALTER TYPE networkoperationtype ADD VALUE IF NOT EXISTS 'autofind_scan'"
            )


def downgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name == "postgresql":
        op.execute(
            "UPDATE network_operations "
            "SET operation_type = 'olt_ont_sync' "
            "WHERE operation_type = 'autofind_scan'"
        )
        # PostgreSQL cannot drop enum values without recreating the type. Keep the
        # value in place after data downgrade to avoid unsafe type surgery.
