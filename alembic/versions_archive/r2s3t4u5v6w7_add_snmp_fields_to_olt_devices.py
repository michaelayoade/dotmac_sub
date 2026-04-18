"""Add snmp_enabled, snmp_port, snmp_version to olt_devices.

Revision ID: r2s3t4u5v6w7
Revises: 7bc16c950bb4
Create Date: 2026-03-17 12:40:00.000000
"""

import sqlalchemy as sa

from alembic import op

revision = "r2s3t4u5v6w7"
down_revision = "7bc16c950bb4"
branch_labels = None
depends_on = None


def upgrade() -> None:
    conn = op.get_bind()
    inspector = sa.inspect(conn)
    existing = {c["name"] for c in inspector.get_columns("olt_devices")}

    if "snmp_enabled" not in existing:
        op.add_column(
            "olt_devices",
            sa.Column(
                "snmp_enabled", sa.Boolean(), server_default="false", nullable=False
            ),
        )
    if "snmp_port" not in existing:
        op.add_column(
            "olt_devices",
            sa.Column("snmp_port", sa.Integer(), server_default="161", nullable=True),
        )
    if "snmp_version" not in existing:
        op.add_column(
            "olt_devices",
            sa.Column(
                "snmp_version", sa.String(10), server_default="v2c", nullable=True
            ),
        )

    # Set snmp_enabled = true where snmp_ro_community is populated (column may not exist)
    existing = {c["name"] for c in inspector.get_columns("olt_devices")}
    if "snmp_ro_community" in existing:
        op.execute(
            "UPDATE olt_devices SET snmp_enabled = true "
            "WHERE snmp_ro_community IS NOT NULL AND snmp_ro_community != ''"
        )


def downgrade() -> None:
    op.drop_column("olt_devices", "snmp_version")
    op.drop_column("olt_devices", "snmp_port")
    op.drop_column("olt_devices", "snmp_enabled")
