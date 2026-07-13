"""Align MikroTik writes with durable operations and readback states.

Revision ID: 274_mikrotik_control_plane_alignment
Revises: 273_communication_suppressions
"""

from __future__ import annotations

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import UUID

from alembic import op

revision = "274_mikrotik_control_plane_alignment"
down_revision = "273_communication_suppressions"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name == "postgresql":
        with op.get_context().autocommit_block():
            op.execute(
                "ALTER TYPE networkoperationtargettype ADD VALUE IF NOT EXISTS 'router'"
            )
            op.execute(
                "ALTER TYPE networkoperationtargettype ADD VALUE IF NOT EXISTS 'nas'"
            )
            op.execute(
                "ALTER TYPE networkoperationtype "
                "ADD VALUE IF NOT EXISTS 'nas_vlan_provision'"
            )
            op.execute(
                "ALTER TYPE routerconfigpushstatus "
                "ADD VALUE IF NOT EXISTS 'pending_readback'"
            )
            op.execute(
                "ALTER TYPE routerpushresultstatus ADD VALUE IF NOT EXISTS 'running'"
            )
            op.execute(
                "ALTER TYPE routerpushresultstatus "
                "ADD VALUE IF NOT EXISTS 'pending_readback'"
            )

    op.add_column(
        "router_config_pushes",
        sa.Column("operation_id", UUID(as_uuid=True), nullable=True),
    )
    op.create_foreign_key(
        "fk_router_config_pushes_operation_id",
        "router_config_pushes",
        "network_operations",
        ["operation_id"],
        ["id"],
        ondelete="SET NULL",
    )
    op.create_index(
        "ix_router_config_pushes_operation_id",
        "router_config_pushes",
        ["operation_id"],
    )
    op.add_column(
        "router_config_push_results",
        sa.Column("operation_id", UUID(as_uuid=True), nullable=True),
    )
    op.create_foreign_key(
        "fk_router_config_push_results_operation_id",
        "router_config_push_results",
        "network_operations",
        ["operation_id"],
        ["id"],
        ondelete="SET NULL",
    )
    op.create_index(
        "ix_router_config_push_results_operation_id",
        "router_config_push_results",
        ["operation_id"],
    )


def downgrade() -> None:
    op.drop_index(
        "ix_router_config_push_results_operation_id",
        table_name="router_config_push_results",
    )
    op.drop_constraint(
        "fk_router_config_push_results_operation_id",
        "router_config_push_results",
        type_="foreignkey",
    )
    op.drop_column("router_config_push_results", "operation_id")
    op.drop_index(
        "ix_router_config_pushes_operation_id",
        table_name="router_config_pushes",
    )
    op.drop_constraint(
        "fk_router_config_pushes_operation_id",
        "router_config_pushes",
        type_="foreignkey",
    )
    op.drop_column("router_config_pushes", "operation_id")
