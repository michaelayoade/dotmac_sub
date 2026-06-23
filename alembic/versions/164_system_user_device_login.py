"""Add device-login (RADIUS) fields to system_users.

Revision ID: 164_system_user_device_login
Revises: 162_drop_olt_circuit_breaker_schema
Create Date: 2026-06-22
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "164_system_user_device_login"
down_revision = "162_drop_olt_circuit_breaker_schema"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "system_users",
        sa.Column(
            "device_login_enabled",
            sa.Boolean(),
            server_default=sa.false(),
            nullable=False,
        ),
    )
    op.add_column(
        "system_users", sa.Column("device_login_secret", sa.String(512), nullable=True)
    )
    op.add_column(
        "system_users",
        sa.Column("device_login_secret_set_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.add_column(
        "system_users",
        sa.Column(
            "device_login_revoked_at", sa.DateTime(timezone=True), nullable=True
        ),
    )


def downgrade() -> None:
    op.drop_column("system_users", "device_login_revoked_at")
    op.drop_column("system_users", "device_login_secret_set_at")
    op.drop_column("system_users", "device_login_secret")
    op.drop_column("system_users", "device_login_enabled")
