"""add device monitoring fields

Revision ID: 3a7f1d2c9e41
Revises: b3c2d9a4f1aa
Create Date: 2026-01-20 11:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = "3a7f1d2c9e41"
down_revision: Union[str, None] = "b3c2d9a4f1aa"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    columns = {col["name"] for col in inspector.get_columns("network_devices")}

    if "device_type" not in columns:
        op.add_column(
            "network_devices",
            sa.Column(
                "device_type",
                sa.Enum(
                    "router",
                    "switch",
                    "hub",
                    "firewall",
                    "inverter",
                    "access_point",
                    "bridge",
                    "modem",
                    "server",
                    "other",
                    name="devicetype",
                ),
                nullable=True,
            ),
        )
    if "ping_enabled" not in columns:
        op.add_column(
            "network_devices",
            sa.Column("ping_enabled", sa.Boolean(), server_default=sa.true(), nullable=False),
        )
    if "snmp_enabled" not in columns:
        op.add_column(
            "network_devices",
            sa.Column("snmp_enabled", sa.Boolean(), server_default=sa.false(), nullable=False),
        )
    if "snmp_port" not in columns:
        op.add_column("network_devices", sa.Column("snmp_port", sa.Integer(), nullable=True))
    if "snmp_version" not in columns:
        op.add_column("network_devices", sa.Column("snmp_version", sa.String(length=10), nullable=True))
    if "snmp_community" not in columns:
        op.add_column("network_devices", sa.Column("snmp_community", sa.String(length=255), nullable=True))
    if "snmp_username" not in columns:
        op.add_column("network_devices", sa.Column("snmp_username", sa.String(length=120), nullable=True))
    if "snmp_auth_protocol" not in columns:
        op.add_column("network_devices", sa.Column("snmp_auth_protocol", sa.String(length=16), nullable=True))
    if "snmp_auth_secret" not in columns:
        op.add_column("network_devices", sa.Column("snmp_auth_secret", sa.String(length=255), nullable=True))
    if "snmp_priv_protocol" not in columns:
        op.add_column("network_devices", sa.Column("snmp_priv_protocol", sa.String(length=16), nullable=True))
    if "snmp_priv_secret" not in columns:
        op.add_column("network_devices", sa.Column("snmp_priv_secret", sa.String(length=255), nullable=True))

    op.execute("UPDATE network_devices SET snmp_port = 161 WHERE snmp_port IS NULL AND snmp_enabled IS TRUE;")
    if "ping_enabled" not in columns:
        op.alter_column("network_devices", "ping_enabled", server_default=None)
    if "snmp_enabled" not in columns:
        op.alter_column("network_devices", "snmp_enabled", server_default=None)


def downgrade() -> None:
    op.drop_column("network_devices", "snmp_priv_secret")
    op.drop_column("network_devices", "snmp_priv_protocol")
    op.drop_column("network_devices", "snmp_auth_secret")
    op.drop_column("network_devices", "snmp_auth_protocol")
    op.drop_column("network_devices", "snmp_username")
    op.drop_column("network_devices", "snmp_community")
    op.drop_column("network_devices", "snmp_version")
    op.drop_column("network_devices", "snmp_port")
    op.drop_column("network_devices", "snmp_enabled")
    op.drop_column("network_devices", "ping_enabled")
    op.drop_column("network_devices", "device_type")
    op.execute("DROP TYPE IF EXISTS devicetype;")
