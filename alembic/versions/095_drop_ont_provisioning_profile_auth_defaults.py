"""Drop legacy ONT provisioning profile auth defaults.

Revision ID: 095_drop_ont_provisioning_profile_auth_defaults
Revises: 094_drop_authorization_preset_profile_defaults
Create Date: 2026-05-06
"""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision = "095_drop_ont_provisioning_profile_auth_defaults"
down_revision = "094_drop_authorization_preset_profile_defaults"
branch_labels = None
depends_on = None


def _column_exists(table_name: str, column_name: str) -> bool:
    inspector = sa.inspect(op.get_bind())
    columns = inspector.get_columns(table_name)
    return any(column["name"] == column_name for column in columns)


def upgrade() -> None:
    if _column_exists("ont_provisioning_profiles", "authorization_line_profile_id"):
        op.drop_column("ont_provisioning_profiles", "authorization_line_profile_id")
    if _column_exists("ont_provisioning_profiles", "authorization_service_profile_id"):
        op.drop_column("ont_provisioning_profiles", "authorization_service_profile_id")


def downgrade() -> None:
    if not _column_exists("ont_provisioning_profiles", "authorization_line_profile_id"):
        op.add_column(
            "ont_provisioning_profiles",
            sa.Column("authorization_line_profile_id", sa.Integer(), nullable=True),
        )
    if not _column_exists(
        "ont_provisioning_profiles", "authorization_service_profile_id"
    ):
        op.add_column(
            "ont_provisioning_profiles",
            sa.Column("authorization_service_profile_id", sa.Integer(), nullable=True),
        )
