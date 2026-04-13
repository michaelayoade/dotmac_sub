"""remove implicit ont profile index defaults

Revision ID: 018_remove_implicit_ont_profile_index_defaults
Revises: 017_add_auth_profiles_to_ont_prov_profiles
Create Date: 2026-04-13
"""

from __future__ import annotations

from alembic import op

revision = "018_remove_implicit_ont_profile_index_defaults"
down_revision = "017_add_auth_profiles_to_ont_prov_profiles"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.alter_column(
        "ont_provisioning_profiles",
        "internet_config_ip_index",
        server_default=None,
    )
    op.alter_column(
        "ont_provisioning_profiles",
        "wan_config_profile_id",
        server_default=None,
    )


def downgrade() -> None:
    op.alter_column(
        "ont_provisioning_profiles",
        "internet_config_ip_index",
        server_default="0",
    )
    op.alter_column(
        "ont_provisioning_profiles",
        "wan_config_profile_id",
        server_default="0",
    )
