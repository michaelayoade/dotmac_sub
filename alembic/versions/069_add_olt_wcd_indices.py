"""Add WAN Connection Device (WCD) index fields to OLT config pack.

Revision ID: 069_wcd_indices
Revises: 068_traffic_tables
Create Date: 2026-04-26

The WCD index determines which WANConnectionDevice.{i} is targeted
in TR-069 paths for different services:
- pppoe_wcd_index: WCD for PPPoE/internet WAN (typically ip-index 1 → WCD2)
- mgmt_wcd_index: WCD for management WAN (typically ip-index 0 → WCD1)

OLT OMCI provisioning determines the WAN container structure on each ONT.
The mapping is: OLT ip-index N → TR-069 WANConnectionDevice.(N+1)
"""

import sqlalchemy as sa

from alembic import op

revision = "069_wcd_indices"
down_revision = "068_traffic_tables"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "olt_devices",
        sa.Column(
            "pppoe_wcd_index",
            sa.Integer(),
            nullable=True,
            comment="WANConnectionDevice index for PPPoE/internet WAN (TR-069)",
        ),
    )
    op.add_column(
        "olt_devices",
        sa.Column(
            "mgmt_wcd_index",
            sa.Integer(),
            nullable=True,
            comment="WANConnectionDevice index for management WAN (TR-069)",
        ),
    )
    op.add_column(
        "olt_devices",
        sa.Column(
            "voip_wcd_index",
            sa.Integer(),
            nullable=True,
            comment="WANConnectionDevice index for VoIP WAN (TR-069)",
        ),
    )


def downgrade() -> None:
    op.drop_column("olt_devices", "voip_wcd_index")
    op.drop_column("olt_devices", "mgmt_wcd_index")
    op.drop_column("olt_devices", "pppoe_wcd_index")
