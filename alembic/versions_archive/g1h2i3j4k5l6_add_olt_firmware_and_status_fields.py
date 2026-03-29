"""Add firmware_version, software_version, status, and file_hash to OLT tables.

Revision ID: g1h2i3j4k5l6
Revises: f88cb663b8e0
Create Date: 2026-03-15
"""

import sqlalchemy as sa

from alembic import op

revision = "g1h2i3j4k5l6"
down_revision = "f88cb663b8e0"
branch_labels = None
depends_on = None


def upgrade() -> None:
    conn = op.get_bind()
    inspector = sa.inspect(conn)
    olt_columns = {col["name"] for col in inspector.get_columns("olt_devices")}
    backup_columns = {col["name"] for col in inspector.get_columns("olt_config_backups")}

    # OLT firmware tracking
    if "firmware_version" not in olt_columns:
        op.add_column(
            "olt_devices",
            sa.Column("firmware_version", sa.String(length=120), nullable=True),
        )
    if "software_version" not in olt_columns:
        op.add_column(
            "olt_devices",
            sa.Column("software_version", sa.String(length=120), nullable=True),
        )

    # OLT device status enum
    devicestatus_enum = sa.Enum(
        "active", "inactive", "maintenance", "retired",
        name="devicestatus",
        create_constraint=False,
    )
    devicestatus_enum.create(conn, checkfirst=True)
    if "status" not in olt_columns:
        op.add_column(
            "olt_devices",
            sa.Column(
                "status",
                devicestatus_enum,
                nullable=True,
                server_default=sa.text("'active'"),
            ),
        )
        # Backfill: active devices get 'active', inactive get 'inactive'
        op.execute("UPDATE olt_devices SET status = 'active' WHERE is_active = TRUE AND status IS NULL")
        op.execute("UPDATE olt_devices SET status = 'inactive' WHERE is_active = FALSE AND status IS NULL")

    # Backup integrity hash
    if "file_hash" not in backup_columns:
        op.add_column(
            "olt_config_backups",
            sa.Column("file_hash", sa.String(length=64), nullable=True),
        )


def downgrade() -> None:
    conn = op.get_bind()
    inspector = sa.inspect(conn)
    olt_columns = {col["name"] for col in inspector.get_columns("olt_devices")}
    backup_columns = {col["name"] for col in inspector.get_columns("olt_config_backups")}

    if "file_hash" in backup_columns:
        op.drop_column("olt_config_backups", "file_hash")
    if "status" in olt_columns:
        op.drop_column("olt_devices", "status")
    if "software_version" in olt_columns:
        op.drop_column("olt_devices", "software_version")
    if "firmware_version" in olt_columns:
        op.drop_column("olt_devices", "firmware_version")
