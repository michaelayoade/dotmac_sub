"""RADIUS session observability: last_update_at + framed IP columns.

last_update_at tracks the most recent accounting observation
(acctupdatetime/acctstoptime) so a live long-running session can be told
apart from a ghost whose Stop was never recorded. Backfilled with
session_start so the reaper has a floor.

framed_ip_address / framed_ipv6_prefix / delegated_ipv6_prefix carry the
addresses FreeRADIUS logs in radacct; previously the importer had nowhere
to put them.

Revision ID: 132_radius_session_observability
Revises: 131_add_crm_subscriber_id
Create Date: 2026-06-09
"""

from __future__ import annotations

import sqlalchemy as sa
from sqlalchemy import inspect

from alembic import op

revision = "132_radius_session_observability"
down_revision = "131_add_crm_subscriber_id"
branch_labels = None
depends_on = None

_TABLE = "radius_accounting_sessions"
_INDEX = "ix_radius_accounting_sessions_open_last_update"
_IP_INDEX = "ix_radius_accounting_sessions_framed_ip"
_NEW_COLUMNS = (
    sa.Column("framed_ip_address", sa.String(64)),
    sa.Column("framed_ipv6_prefix", sa.String(128)),
    sa.Column("delegated_ipv6_prefix", sa.String(128)),
    sa.Column("nas_port_id", sa.String(64)),
    sa.Column("called_station_id", sa.String(64)),
)


def upgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name == "sqlite":
        return

    inspector = inspect(bind)
    if _TABLE not in inspector.get_table_names():
        return
    columns = {item["name"] for item in inspector.get_columns(_TABLE)}
    if "last_update_at" not in columns:
        op.add_column(_TABLE, sa.Column("last_update_at", sa.DateTime(timezone=True)))
        op.execute(
            f"UPDATE {_TABLE} SET last_update_at = session_start "
            "WHERE last_update_at IS NULL"
        )
    for column in _NEW_COLUMNS:
        if column.name not in columns:
            op.add_column(_TABLE, column)
    indexes = {item["name"] for item in inspector.get_indexes(_TABLE)}
    if _INDEX not in indexes:
        # Partial index over open sessions — the reaper's working set.
        op.create_index(
            _INDEX,
            _TABLE,
            ["last_update_at"],
            postgresql_where=sa.text("session_end IS NULL"),
        )
    if _IP_INDEX not in indexes:
        # Reverse lookup: who held this IP at time T.
        op.create_index(_IP_INDEX, _TABLE, ["framed_ip_address"])


def downgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name == "sqlite":
        return
    op.drop_index(_IP_INDEX, table_name=_TABLE)
    op.drop_index(_INDEX, table_name=_TABLE)
    for column in _NEW_COLUMNS:
        op.drop_column(_TABLE, column.name)
    op.drop_column(_TABLE, "last_update_at")
