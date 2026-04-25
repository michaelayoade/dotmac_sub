"""Add service config fields to ont_assignments

Revision ID: 066_add_service_config
Revises: 065_add_ont_desired_config_drop_overrides
Create Date: 2025-04-25

Service configuration (WAN mode, IP mode, PPPoE, WiFi) now lives on
OntAssignment instead of OntUnit.desired_config. This keeps Subscriber
independent from network config while linking service settings to where
subscriber meets device.
"""

from alembic import op
import sqlalchemy as sa


revision = "066_add_service_config"
down_revision = "065_add_ont_desired_config_drop_overrides"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Add service config columns to ont_assignments
    op.add_column(
        "ont_assignments",
        sa.Column(
            "wan_mode",
            sa.String(20),
            nullable=True,
            comment="WAN mode: routing or bridging",
        ),
    )
    op.add_column(
        "ont_assignments",
        sa.Column(
            "ip_mode",
            sa.String(20),
            nullable=True,
            comment="IP mode: dhcp, static_ip, or inactive",
        ),
    )
    op.add_column(
        "ont_assignments",
        sa.Column(
            "static_ip",
            sa.String(64),
            nullable=True,
            comment="Static IP address",
        ),
    )
    op.add_column(
        "ont_assignments",
        sa.Column(
            "static_gateway",
            sa.String(64),
            nullable=True,
            comment="Static gateway",
        ),
    )
    op.add_column(
        "ont_assignments",
        sa.Column(
            "static_subnet",
            sa.String(64),
            nullable=True,
            comment="Static subnet mask",
        ),
    )
    op.add_column(
        "ont_assignments",
        sa.Column(
            "pppoe_username",
            sa.String(200),
            nullable=True,
            comment="PPPoE username",
        ),
    )
    op.add_column(
        "ont_assignments",
        sa.Column(
            "pppoe_password",
            sa.String(512),
            nullable=True,
            comment="PPPoE password (encrypted)",
        ),
    )
    op.add_column(
        "ont_assignments",
        sa.Column(
            "wifi_ssid",
            sa.String(64),
            nullable=True,
            comment="WiFi SSID",
        ),
    )
    op.add_column(
        "ont_assignments",
        sa.Column(
            "wifi_password",
            sa.String(512),
            nullable=True,
            comment="WiFi password (encrypted)",
        ),
    )

    # Set defaults for existing rows
    op.execute(
        "UPDATE ont_assignments SET wan_mode = 'routing', ip_mode = 'dhcp' WHERE wan_mode IS NULL"
    )


def downgrade() -> None:
    op.drop_column("ont_assignments", "wifi_password")
    op.drop_column("ont_assignments", "wifi_ssid")
    op.drop_column("ont_assignments", "pppoe_password")
    op.drop_column("ont_assignments", "pppoe_username")
    op.drop_column("ont_assignments", "static_subnet")
    op.drop_column("ont_assignments", "static_gateway")
    op.drop_column("ont_assignments", "static_ip")
    op.drop_column("ont_assignments", "ip_mode")
    op.drop_column("ont_assignments", "wan_mode")
