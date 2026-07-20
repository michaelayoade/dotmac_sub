"""Add Huawei ONT IPv6 observation fields.

Revision ID: 262_huawei_ont_observations
Revises: 261_system_user_role_source
"""

import sqlalchemy as sa

from alembic import op

revision = "262_huawei_ont_observations"
down_revision = "261_system_user_role_source"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("ont_observations", sa.Column("acs_data_model_root", sa.String(40)))
    op.add_column(
        "ont_observations", sa.Column("acs_observed_ipv6_enabled", sa.Boolean())
    )
    op.add_column(
        "ont_observations", sa.Column("acs_observed_wan_ip_enable", sa.Boolean())
    )
    op.add_column(
        "ont_observations", sa.Column("acs_observed_wan_addressing_type", sa.String(20))
    )
    op.add_column(
        "ont_observations", sa.Column("acs_observed_wan_ip_address", sa.String(64))
    )
    op.add_column(
        "ont_observations", sa.Column("acs_observed_wan_subnet_mask", sa.String(64))
    )
    op.add_column(
        "ont_observations", sa.Column("acs_observed_wan_gateway", sa.String(64))
    )
    op.add_column(
        "ont_observations", sa.Column("acs_observed_dhcpv6_enabled", sa.Boolean())
    )
    op.add_column(
        "ont_observations",
        sa.Column("acs_observed_dhcpv6_request_prefixes", sa.Boolean()),
    )
    op.add_column(
        "ont_observations", sa.Column("acs_observed_ra_enabled", sa.Boolean())
    )


def downgrade() -> None:
    op.drop_column("ont_observations", "acs_observed_ra_enabled")
    op.drop_column("ont_observations", "acs_observed_dhcpv6_request_prefixes")
    op.drop_column("ont_observations", "acs_observed_dhcpv6_enabled")
    op.drop_column("ont_observations", "acs_observed_wan_gateway")
    op.drop_column("ont_observations", "acs_observed_wan_subnet_mask")
    op.drop_column("ont_observations", "acs_observed_wan_ip_address")
    op.drop_column("ont_observations", "acs_observed_wan_addressing_type")
    op.drop_column("ont_observations", "acs_observed_wan_ip_enable")
    op.drop_column("ont_observations", "acs_observed_ipv6_enabled")
    op.drop_column("ont_observations", "acs_data_model_root")
