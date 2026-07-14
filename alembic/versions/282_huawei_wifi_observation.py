"""Persist reconciled Huawei WiFi observations.

Revision ID: 282_huawei_wifi_observation
Revises: 281_tr181_wan_observed_dns
"""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision = "282_huawei_wifi_observation"
down_revision = "281_tr181_wan_observed_dns"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "ont_observations",
        sa.Column("acs_observed_wifi_enabled", sa.Boolean()),
    )
    op.add_column(
        "ont_observations",
        sa.Column("acs_observed_wifi_channel", sa.Integer()),
    )
    op.add_column(
        "ont_observations",
        sa.Column("acs_observed_wifi_security_mode", sa.String(length=40)),
    )
    op.add_column(
        "ont_observations",
        sa.Column("acs_observed_wifi_instance_index", sa.Integer()),
    )


def downgrade() -> None:
    op.drop_column("ont_observations", "acs_observed_wifi_instance_index")
    op.drop_column("ont_observations", "acs_observed_wifi_security_mode")
    op.drop_column("ont_observations", "acs_observed_wifi_channel")
    op.drop_column("ont_observations", "acs_observed_wifi_enabled")
