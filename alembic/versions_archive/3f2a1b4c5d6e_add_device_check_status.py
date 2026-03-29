"""Add ping/snmp check status fields to network devices.

Revision ID: 3f2a1b4c5d6e
Revises: c7d8a9b0e1f2
Create Date: 2026-01-19 12:40:00.000000
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = "3f2a1b4c5d6e"
down_revision: Union[str, None] = "c7d8a9b0e1f2"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("network_devices", sa.Column("last_ping_at", sa.DateTime(timezone=True), nullable=True))
    op.add_column("network_devices", sa.Column("last_ping_ok", sa.Boolean(), nullable=True))
    op.add_column("network_devices", sa.Column("last_snmp_at", sa.DateTime(timezone=True), nullable=True))
    op.add_column("network_devices", sa.Column("last_snmp_ok", sa.Boolean(), nullable=True))


def downgrade() -> None:
    op.drop_column("network_devices", "last_snmp_ok")
    op.drop_column("network_devices", "last_snmp_at")
    op.drop_column("network_devices", "last_ping_ok")
    op.drop_column("network_devices", "last_ping_at")
