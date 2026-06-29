"""Add MFA recovery codes.

Revision ID: 186_mfa_recovery_codes
Revises: 185_router_rest_api_username_width
"""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision = "186_mfa_recovery_codes"
down_revision = "185_router_rest_api_username_width"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "mfa_recovery_codes",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("mfa_method_id", sa.UUID(), nullable=False),
        sa.Column("code_hash", sa.String(length=255), nullable=False),
        sa.Column("is_active", sa.Boolean(), nullable=False),
        sa.Column("used_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["mfa_method_id"], ["mfa_methods.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_mfa_recovery_codes_method_active",
        "mfa_recovery_codes",
        ["mfa_method_id", "is_active"],
    )
    op.create_index(
        "ux_mfa_recovery_codes_code_hash",
        "mfa_recovery_codes",
        ["code_hash"],
        unique=True,
    )


def downgrade() -> None:
    op.drop_index("ux_mfa_recovery_codes_code_hash", table_name="mfa_recovery_codes")
    op.drop_index(
        "ix_mfa_recovery_codes_method_active", table_name="mfa_recovery_codes"
    )
    op.drop_table("mfa_recovery_codes")
