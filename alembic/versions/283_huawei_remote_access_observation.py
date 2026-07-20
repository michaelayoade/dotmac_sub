"""Persist reconciled Huawei remote-access observations.

Revision ID: 283_huawei_remote_access_observation
Revises: 282_huawei_wifi_observation
"""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision = "283_huawei_remote_access_observation"
down_revision = "282_huawei_wifi_observation"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "ont_observations",
        sa.Column("acs_observed_remote_ssh_enabled", sa.Boolean()),
    )
    op.add_column(
        "ont_observations",
        sa.Column("acs_observed_remote_ssh_port", sa.Integer()),
    )
    op.add_column(
        "ont_observations",
        sa.Column("acs_observed_remote_telnet_enabled", sa.Boolean()),
    )
    op.add_column(
        "ont_observations",
        sa.Column("acs_observed_remote_telnet_port", sa.Integer()),
    )


def downgrade() -> None:
    op.drop_column("ont_observations", "acs_observed_remote_telnet_port")
    op.drop_column("ont_observations", "acs_observed_remote_telnet_enabled")
    op.drop_column("ont_observations", "acs_observed_remote_ssh_port")
    op.drop_column("ont_observations", "acs_observed_remote_ssh_enabled")
