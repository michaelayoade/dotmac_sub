"""Allow full-length RADIUS NAS-Port-Id values in app session projections.

Revision ID: 272_radius_nas_port_id_capacity
Revises: 271_billing_portal_read_pressure_indexes
"""

from __future__ import annotations

import sqlalchemy as sa
from sqlalchemy import inspect

from alembic import op

revision = "272_radius_nas_port_id_capacity"
down_revision = "271_billing_portal_read_pressure_indexes"
branch_labels = None
depends_on = None

_TABLES = ("radius_accounting_sessions", "radius_active_sessions")


def _has_nas_port_id(table_name: str) -> bool:
    inspector = inspect(op.get_bind())
    return table_name in inspector.get_table_names() and any(
        column["name"] == "nas_port_id" for column in inspector.get_columns(table_name)
    )


def upgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name == "sqlite":
        return
    for table_name in _TABLES:
        if _has_nas_port_id(table_name):
            op.alter_column(
                table_name,
                "nas_port_id",
                type_=sa.String(length=253),
                existing_nullable=True,
            )


def downgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name == "sqlite":
        return
    for table_name, previous_length in (
        ("radius_accounting_sessions", 64),
        ("radius_active_sessions", 120),
    ):
        if _has_nas_port_id(table_name):
            op.alter_column(
                table_name,
                "nas_port_id",
                type_=sa.String(length=previous_length),
                existing_nullable=True,
            )
