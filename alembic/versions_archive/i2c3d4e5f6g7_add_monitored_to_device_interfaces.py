"""Add monitored flag to device_interfaces.

Controls which interfaces are polled for bandwidth counters.
Only monitored=True interfaces are included in SNMP traffic polling.

Revision ID: i2c3d4e5f6g7
Revises: h1b2c3d4e5f6
Create Date: 2026-03-22
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "i2c3d4e5f6g7"
down_revision: str = "h1b2c3d4e5f6"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    conn = op.get_bind()
    inspector = sa.inspect(conn)
    columns = [c["name"] for c in inspector.get_columns("device_interfaces")]
    if "monitored" not in columns:
        op.add_column(
            "device_interfaces",
            sa.Column(
                "monitored",
                sa.Boolean(),
                server_default=sa.false(),
                nullable=False,
            ),
        )

    # Auto-enable monitoring on common uplink/subscriber interface patterns
    op.execute("""
        UPDATE device_interfaces
        SET monitored = true
        WHERE monitored = false
          AND status = 'up'
          AND snmp_index IS NOT NULL
          AND (
              name ILIKE 'sfp%'
              OR name ILIKE 'ether%'
              OR name ILIKE '%pppoe%'
              OR name ILIKE 'GigabitEthernet%'
              OR name ILIKE 'TenGigabitEthernet%'
              OR name ILIKE 'xe-%'
              OR name ILIKE 'et-%'
          )
    """)


def downgrade() -> None:
    op.drop_column("device_interfaces", "monitored")
