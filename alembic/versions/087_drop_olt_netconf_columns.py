"""Drop OLT netconf columns.

NETCONF was never deployed on the OLTs, so these columns are unused.

Revision ID: 087_drop_olt_netconf_columns
Revises: 086_move_ont_access_flags
Create Date: 2026-05-06
"""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision = "087_drop_olt_netconf_columns"
down_revision = "086_move_ont_access_flags"
branch_labels = None
depends_on = None


def _column_exists(inspector: sa.Inspector, table: str, column: str) -> bool:
    return any(col["name"] == column for col in inspector.get_columns(table))


def _drop_column_if_exists(inspector: sa.Inspector, table: str, column: str) -> None:
    if _column_exists(inspector, table, column):
        op.drop_column(table, column)


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)

    _drop_column_if_exists(inspector, "olt_devices", "netconf_enabled")
    _drop_column_if_exists(inspector, "olt_devices", "netconf_port")


def downgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)

    if not _column_exists(inspector, "olt_devices", "netconf_enabled"):
        op.add_column(
            "olt_devices",
            sa.Column(
                "netconf_enabled",
                sa.Boolean(),
                nullable=False,
                server_default="false",
            ),
        )
    if not _column_exists(inspector, "olt_devices", "netconf_port"):
        op.add_column(
            "olt_devices",
            sa.Column(
                "netconf_port",
                sa.Integer(),
                nullable=True,
                server_default="830",
            ),
        )
