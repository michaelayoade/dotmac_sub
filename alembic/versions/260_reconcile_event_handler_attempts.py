"""Reconcile legacy event handler attempt table shapes.

Revision ID: 260_reconcile_event_attempts
Revises: 259_campaign_ai_workqueue
Create Date: 2026-07-12
"""

from __future__ import annotations

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision = "260_reconcile_event_attempts"
down_revision = "259_campaign_ai_workqueue"
branch_labels = None
depends_on = None

_TABLE = "event_handler_attempts"


def _inspector():
    return sa.inspect(op.get_bind())


def _columns() -> set[str]:
    return {column["name"] for column in _inspector().get_columns(_TABLE)}


def _indexes() -> set[str]:
    return {index["name"] for index in _inspector().get_indexes(_TABLE)}


def _create_canonical_table() -> None:
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


def upgrade() -> None:
    if op.get_bind().dialect.name == "sqlite":
        return
    if _TABLE not in _inspector().get_table_names():
        _create_canonical_table()
    else:
        columns = _columns()
        if "attempted_at" not in columns:
            op.add_column(
                _TABLE,
                sa.Column("attempted_at", sa.DateTime(timezone=True), nullable=True),
            )
            columns.add("attempted_at")

        timestamp_sources = [
            name for name in ("processed_at", "created_at") if name in columns
        ]
        fallback = "COALESCE(" + ", ".join((*timestamp_sources, "now()")) + ")"
        op.execute(
            sa.text(
                f"UPDATE {_TABLE} SET attempted_at = {fallback} "
                "WHERE attempted_at IS NULL"
            )
        )
        op.alter_column(_TABLE, "attempted_at", nullable=False)

        # These belonged to the pre-251 table and are not mapped by the current
        # model. Their timestamps have been preserved in attempted_at above.
        for legacy_column in ("processed_at", "created_at"):
            if legacy_column in columns:
                op.drop_column(_TABLE, legacy_column)

    for index_name, columns in (
        ("ix_event_handler_attempts_event_store_id", ["event_store_id"]),
        ("ix_event_handler_attempts_handler_name", ["handler_name"]),
        ("ix_event_handler_attempts_status", ["status"]),
    ):
        if index_name not in _indexes():
            op.create_index(index_name, _TABLE, columns)


def downgrade() -> None:
    # The down revision already intends this canonical schema. Restoring the
    # incompatible legacy columns would reintroduce production insert failures.
    pass
