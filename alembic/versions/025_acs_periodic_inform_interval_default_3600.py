"""Align tr069_acs_servers.periodic_inform_interval default to fleet value 3600s.

Revision ID: 025_acs_interval_3600
Revises: 024_add_wifi_ssid_password_to_ont_units
Create Date: 2026-04-16

"""

from alembic import op

revision = "025_acs_interval_3600"
down_revision = "024_add_wifi_ssid_password_to_ont_units"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.alter_column(
        "tr069_acs_servers",
        "periodic_inform_interval",
        server_default="3600",
    )


def downgrade() -> None:
    op.alter_column(
        "tr069_acs_servers",
        "periodic_inform_interval",
        server_default="300",
    )
