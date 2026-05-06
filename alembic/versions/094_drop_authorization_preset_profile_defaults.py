"""Drop legacy authorization preset profile defaults.

Revision ID: 094_drop_authorization_preset_profile_defaults
Revises: 093_clear_ont_chassis_model_values
Create Date: 2026-05-06
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "094_drop_authorization_preset_profile_defaults"
down_revision = "093_clear_ont_chassis_model_values"
branch_labels = None
depends_on = None


def _column_exists(table_name: str, column_name: str) -> bool:
    inspector = sa.inspect(op.get_bind())
    return any(column["name"] == column_name for column in inspector.get_columns(table_name))


def upgrade() -> None:
    if _column_exists("authorization_presets", "line_profile_id"):
        op.drop_column("authorization_presets", "line_profile_id")
    if _column_exists("authorization_presets", "service_profile_id"):
        op.drop_column("authorization_presets", "service_profile_id")


def downgrade() -> None:
    if not _column_exists("authorization_presets", "line_profile_id"):
        op.add_column(
            "authorization_presets",
            sa.Column("line_profile_id", sa.Integer(), nullable=True),
        )
    if not _column_exists("authorization_presets", "service_profile_id"):
        op.add_column(
            "authorization_presets",
            sa.Column("service_profile_id", sa.Integer(), nullable=True),
        )
