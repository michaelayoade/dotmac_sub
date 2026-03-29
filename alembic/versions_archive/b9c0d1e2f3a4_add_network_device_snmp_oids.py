"""Add network device SNMP OID table.

Revision ID: b9c0d1e2f3a4
Revises: z6a7b8c9d0e1
Create Date: 2026-02-25
"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy import inspect
from sqlalchemy.dialects.postgresql import UUID

from alembic import op

revision: str = "b9c0d1e2f3a4"
down_revision: str | Sequence[str] | None = "z6a7b8c9d0e1"
branch_labels: Sequence[str] | None = None
depends_on: Sequence[str] | None = None


def upgrade() -> None:
    bind = op.get_bind()
    inspector = inspect(bind)
    if inspector.has_table("network_device_snmp_oids"):
        return
    op.create_table(
        "network_device_snmp_oids",
        sa.Column("id", UUID(as_uuid=True), primary_key=True),
        sa.Column("device_id", UUID(as_uuid=True), sa.ForeignKey("network_devices.id"), nullable=False),
        sa.Column("title", sa.String(length=200), nullable=False),
        sa.Column("oid", sa.String(length=160), nullable=False),
        sa.Column("check_interval_seconds", sa.Integer(), nullable=False, server_default=sa.text("60")),
        sa.Column("rrd_data_source_type", sa.String(length=16), nullable=False, server_default="gauge"),
        sa.Column("is_enabled", sa.Boolean(), nullable=False, server_default=sa.text("true")),
        sa.Column("last_poll_status", sa.String(length=16), nullable=True),
        sa.Column("last_polled_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_error", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
    )
    op.create_index(
        "ix_network_device_snmp_oids_device_id",
        "network_device_snmp_oids",
        ["device_id"],
        unique=False,
    )


def downgrade() -> None:
    bind = op.get_bind()
    inspector = inspect(bind)
    if not inspector.has_table("network_device_snmp_oids"):
        return
    op.drop_index("ix_network_device_snmp_oids_device_id", table_name="network_device_snmp_oids")
    op.drop_table("network_device_snmp_oids")
