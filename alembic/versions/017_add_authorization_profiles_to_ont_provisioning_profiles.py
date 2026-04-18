"""add authorization profiles to ont provisioning profiles

Revision ID: 017_add_auth_profiles_to_ont_prov_profiles
Revises: 016_scope_vlan_uniqueness_to_olt
Create Date: 2026-04-13
"""

from __future__ import annotations

import sqlalchemy as sa
from sqlalchemy import inspect

from alembic import op

revision = "017_add_auth_profiles_to_ont_prov_profiles"
down_revision = "016_scope_vlan_uniqueness_to_olt"
branch_labels = None
depends_on = None


def _has_column(table_name: str, column_name: str) -> bool:
    inspector = inspect(op.get_bind())
    return any(
        column["name"] == column_name for column in inspector.get_columns(table_name)
    )


def upgrade() -> None:
    if not _has_column("ont_provisioning_profiles", "authorization_line_profile_id"):
        op.add_column(
            "ont_provisioning_profiles",
            sa.Column("authorization_line_profile_id", sa.Integer(), nullable=True),
        )
    if not _has_column("ont_provisioning_profiles", "authorization_service_profile_id"):
        op.add_column(
            "ont_provisioning_profiles",
            sa.Column("authorization_service_profile_id", sa.Integer(), nullable=True),
        )


def downgrade() -> None:
    if _has_column("ont_provisioning_profiles", "authorization_service_profile_id"):
        op.drop_column("ont_provisioning_profiles", "authorization_service_profile_id")
    if _has_column("ont_provisioning_profiles", "authorization_line_profile_id"):
        op.drop_column("ont_provisioning_profiles", "authorization_line_profile_id")
