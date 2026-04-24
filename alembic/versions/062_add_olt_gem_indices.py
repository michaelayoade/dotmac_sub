"""Add GEM index fields to OLTDevice for Config Pack

Revision ID: 062_add_olt_gem_indices
Revises: 061_add_acs_config_pack_and_mgmt_ip_pool
Create Date: 2026-04-24

GEM (GPON Encapsulation Method) ports map traffic types to transport containers.
This migration adds per-purpose GEM index defaults to OLT Config Pack:
- Internet: typically GEM 1
- Management/TR-069: typically GEM 2
- VoIP: typically GEM 3
- IPTV: typically GEM 4
"""

from alembic import op
import sqlalchemy as sa

revision = "062_add_olt_gem_indices"
down_revision = "061_add_acs_config_pack_and_mgmt_ip_pool"
branch_labels = None
depends_on = None


def upgrade() -> None:
    conn = op.get_bind()
    inspector = sa.inspect(conn)

    olt_columns = [col["name"] for col in inspector.get_columns("olt_devices")]

    # GEM index for internet service ports
    if "default_internet_gem_index" not in olt_columns:
        op.add_column(
            "olt_devices",
            sa.Column(
                "default_internet_gem_index",
                sa.Integer(),
                nullable=True,
                server_default="1",
                comment="GEM index for internet service ports (typically 1)",
            ),
        )

    # GEM index for management/TR-069 service ports
    if "default_mgmt_gem_index" not in olt_columns:
        op.add_column(
            "olt_devices",
            sa.Column(
                "default_mgmt_gem_index",
                sa.Integer(),
                nullable=True,
                server_default="2",
                comment="GEM index for management/TR-069 service ports (typically 2)",
            ),
        )

    # GEM index for VoIP service ports
    if "default_voip_gem_index" not in olt_columns:
        op.add_column(
            "olt_devices",
            sa.Column(
                "default_voip_gem_index",
                sa.Integer(),
                nullable=True,
                server_default="3",
                comment="GEM index for VoIP service ports (typically 3)",
            ),
        )

    # GEM index for IPTV service ports
    if "default_iptv_gem_index" not in olt_columns:
        op.add_column(
            "olt_devices",
            sa.Column(
                "default_iptv_gem_index",
                sa.Integer(),
                nullable=True,
                server_default="4",
                comment="GEM index for IPTV service ports (typically 4)",
            ),
        )


def downgrade() -> None:
    conn = op.get_bind()
    inspector = sa.inspect(conn)

    olt_columns = [col["name"] for col in inspector.get_columns("olt_devices")]

    columns_to_drop = [
        "default_internet_gem_index",
        "default_mgmt_gem_index",
        "default_voip_gem_index",
        "default_iptv_gem_index",
    ]

    for col in columns_to_drop:
        if col in olt_columns:
            op.drop_column("olt_devices", col)
