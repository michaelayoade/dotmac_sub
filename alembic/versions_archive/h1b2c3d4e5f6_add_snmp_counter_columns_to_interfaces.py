"""Add SNMP counter columns to device_interfaces.

Adds snmp_index, last_in_octets, last_out_octets, last_counter_at
for computing bandwidth bps deltas from SNMP interface counter polling.

Revision ID: h1b2c3d4e5f6
Revises: g0a1b2c3d4e5
Create Date: 2026-03-22
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "h1b2c3d4e5f6"
down_revision: str = "g0a1b2c3d4e5"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    conn = op.get_bind()
    inspector = sa.inspect(conn)
    columns = [c["name"] for c in inspector.get_columns("device_interfaces")]

    if "snmp_index" not in columns:
        op.add_column("device_interfaces", sa.Column("snmp_index", sa.BigInteger(), nullable=True))
    if "last_in_octets" not in columns:
        op.add_column("device_interfaces", sa.Column("last_in_octets", sa.Float(), nullable=True))
    if "last_out_octets" not in columns:
        op.add_column("device_interfaces", sa.Column("last_out_octets", sa.Float(), nullable=True))
    if "last_counter_at" not in columns:
        op.add_column("device_interfaces", sa.Column("last_counter_at", sa.DateTime(timezone=True), nullable=True))


def downgrade() -> None:
    op.drop_column("device_interfaces", "last_counter_at")
    op.drop_column("device_interfaces", "last_out_octets")
    op.drop_column("device_interfaces", "last_in_octets")
    op.drop_column("device_interfaces", "snmp_index")
