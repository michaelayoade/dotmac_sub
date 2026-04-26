"""Add traffic table index fields to OLT config pack.

Revision ID: 068_traffic_tables
Revises: 067_add_vlan_and_mgmt_ip_to_assignment
Create Date: 2026-04-26

These fields store OLT-specific traffic-table indices used in service-port
commands for QoS binding. Values are extracted from OLT running configs.
"""

import sqlalchemy as sa

from alembic import op

revision = "068_traffic_tables"
down_revision = "067_add_vlan_and_mgmt_ip_to_assignment"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "olt_devices",
        sa.Column(
            "mgmt_traffic_table_inbound",
            sa.Integer(),
            nullable=True,
            comment="Inbound traffic-table index for VLAN 201 (management) service-ports",
        ),
    )
    op.add_column(
        "olt_devices",
        sa.Column(
            "mgmt_traffic_table_outbound",
            sa.Integer(),
            nullable=True,
            comment="Outbound traffic-table index for VLAN 201 (management) service-ports",
        ),
    )
    op.add_column(
        "olt_devices",
        sa.Column(
            "internet_traffic_table_inbound",
            sa.Integer(),
            nullable=True,
            comment="Inbound traffic-table index for VLAN 203 (PPPoE/internet) service-ports",
        ),
    )
    op.add_column(
        "olt_devices",
        sa.Column(
            "internet_traffic_table_outbound",
            sa.Integer(),
            nullable=True,
            comment="Outbound traffic-table index for VLAN 203 (PPPoE/internet) service-ports",
        ),
    )


def downgrade() -> None:
    op.drop_column("olt_devices", "internet_traffic_table_outbound")
    op.drop_column("olt_devices", "internet_traffic_table_inbound")
    op.drop_column("olt_devices", "mgmt_traffic_table_outbound")
    op.drop_column("olt_devices", "mgmt_traffic_table_inbound")
