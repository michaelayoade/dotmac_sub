"""Track the authority that assigned a system-user role.

Revision ID: 261_system_user_role_source
Revises: 260_reconcile_event_attempts
Create Date: 2026-07-12
"""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision = "261_system_user_role_source"
down_revision = "260_reconcile_event_attempts"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "system_user_roles",
        sa.Column(
            "source",
            sa.String(length=40),
            nullable=False,
            server_default="local",
        ),
    )
    op.create_index(
        "ix_system_user_roles_user_source",
        "system_user_roles",
        ["system_user_id", "source"],
    )


def downgrade() -> None:
    op.drop_index("ix_system_user_roles_user_source", table_name="system_user_roles")
    op.drop_column("system_user_roles", "source")
