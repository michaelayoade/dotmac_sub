"""Add first-class event handler attempt rows.

Revision ID: 251_event_handler_attempts
Revises: 250_field_material_request_erp_fields
Create Date: 2026-07-11
"""

from __future__ import annotations

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision = "251_event_handler_attempts"
down_revision = "250_field_material_request_erp_fields"
branch_labels = None
depends_on = None

_TABLE = "event_handler_attempts"


def _inspector():
    return sa.inspect(op.get_bind())


def _has_table(table_name: str) -> bool:
    return table_name in _inspector().get_table_names()


def _has_index(table_name: str, index_name: str) -> bool:
    return index_name in {ix["name"] for ix in _inspector().get_indexes(table_name)}


def upgrade() -> None:
    if op.get_bind().dialect.name == "sqlite":
        return
    if _has_table(_TABLE):
        return

    op.create_table(
        _TABLE,
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "event_store_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("event_store.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("handler_name", sa.String(length=160), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("error", sa.Text()),
        sa.Column("retry_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("attempted_at", sa.DateTime(timezone=True), nullable=False),
    )
    for index_name, columns in (
        ("ix_event_handler_attempts_event_store_id", ["event_store_id"]),
        ("ix_event_handler_attempts_handler_name", ["handler_name"]),
        ("ix_event_handler_attempts_status", ["status"]),
    ):
        if not _has_index(_TABLE, index_name):
            op.create_index(index_name, _TABLE, columns)


def downgrade() -> None:
    if op.get_bind().dialect.name == "sqlite":
        return
    if not _has_table(_TABLE):
        return
    for index_name in (
        "ix_event_handler_attempts_status",
        "ix_event_handler_attempts_handler_name",
        "ix_event_handler_attempts_event_store_id",
    ):
        if _has_index(_TABLE, index_name):
            op.drop_index(index_name, table_name=_TABLE)
    op.drop_table(_TABLE)
