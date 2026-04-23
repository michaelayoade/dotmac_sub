"""Add component metadata to WAN service instances.

Revision ID: 053_add_wan_instance_components
Revises: 052_remove_legacy_tr069_step_types
Create Date: 2026-04-23
"""

from __future__ import annotations

import sqlalchemy as sa
from sqlalchemy import inspect

from alembic import op

revision = "053_add_wan_instance_components"
down_revision = "052_remove_legacy_tr069_step_types"
branch_labels = None
depends_on = None

_TABLE = "ont_wan_service_instances"


def _has_table(conn) -> bool:
    return _TABLE in inspect(conn).get_table_names()


def _columns(conn) -> set[str]:
    return {column["name"] for column in inspect(conn).get_columns(_TABLE)}


def _add_column_if_missing(conn, name: str, column: sa.Column) -> None:
    if name not in _columns(conn):
        op.add_column(_TABLE, column)


def upgrade() -> None:
    conn = op.get_bind()
    if not _has_table(conn):
        return

    _add_column_if_missing(conn, "cos_priority", sa.Column("cos_priority", sa.Integer()))
    _add_column_if_missing(
        conn,
        "mtu",
        sa.Column("mtu", sa.Integer(), nullable=False, server_default="1500"),
    )
    _add_column_if_missing(
        conn,
        "ip_mode",
        sa.Column(
            "ip_mode",
            sa.Enum(
                "ipv4",
                "dual_stack",
                name="ipprotocol",
                create_type=False,
            ),
        ),
    )
    _add_column_if_missing(
        conn, "static_ip_source", sa.Column("static_ip_source", sa.String(200))
    )
    _add_column_if_missing(conn, "bind_lan_ports", sa.Column("bind_lan_ports", sa.JSON()))
    _add_column_if_missing(
        conn, "bind_ssid_index", sa.Column("bind_ssid_index", sa.Integer())
    )
    _add_column_if_missing(conn, "gem_port_id", sa.Column("gem_port_id", sa.Integer()))
    _add_column_if_missing(
        conn, "t_cont_profile", sa.Column("t_cont_profile", sa.String(120))
    )
    op.alter_column(_TABLE, "mtu", server_default=None)


def downgrade() -> None:
    conn = op.get_bind()
    if not _has_table(conn):
        return

    columns = _columns(conn)
    for name in (
        "t_cont_profile",
        "gem_port_id",
        "bind_ssid_index",
        "bind_lan_ports",
        "static_ip_source",
        "ip_mode",
        "mtu",
        "cos_priority",
    ):
        if name in columns:
            op.drop_column(_TABLE, name)
