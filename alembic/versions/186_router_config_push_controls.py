"""Add router config push operator controls.

Revision ID: 186_router_config_push_controls
Revises: 185_router_rest_api_username_width
"""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision = "186_router_config_push_controls"
down_revision = "185_router_rest_api_username_width"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "router_config_pushes",
        sa.Column("dry_run", sa.Boolean(), nullable=False, server_default=sa.false()),
    )
    op.add_column(
        "router_config_pushes",
        sa.Column(
            "failure_policy",
            sa.String(length=20),
            nullable=False,
            server_default="continue",
        ),
    )
    op.add_column(
        "router_config_pushes",
        sa.Column(
            "allow_dangerous_commands",
            sa.Boolean(),
            nullable=False,
            server_default=sa.false(),
        ),
    )
    op.alter_column("router_config_pushes", "dry_run", server_default=None)
    op.alter_column("router_config_pushes", "failure_policy", server_default=None)
    op.alter_column(
        "router_config_pushes", "allow_dangerous_commands", server_default=None
    )


def downgrade() -> None:
    op.drop_column("router_config_pushes", "allow_dangerous_commands")
    op.drop_column("router_config_pushes", "failure_policy")
    op.drop_column("router_config_pushes", "dry_run")
