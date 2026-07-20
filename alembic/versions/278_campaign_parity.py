"""Campaign parity: send windows, sequence steps, and delivery tracking.

Revision ID: 278_campaign_parity
Revises: 277_lifecycle_comms_sot
Create Date: 2026-07-12
"""

from __future__ import annotations

import sqlalchemy as sa
from sqlalchemy import inspect

from alembic import op

revision = "278_campaign_parity"
down_revision = "277_lifecycle_comms_sot"
branch_labels = None
depends_on = None


def _has_table(name: str) -> bool:
    return name in inspect(op.get_bind()).get_table_names()


def _has_column(table_name: str, column_name: str) -> bool:
    if not _has_table(table_name):
        return False
    return column_name in {
        column["name"] for column in inspect(op.get_bind()).get_columns(table_name)
    }


def _has_index(table_name: str, index_name: str) -> bool:
    if not _has_table(table_name):
        return False
    return index_name in {
        index["name"] for index in inspect(op.get_bind()).get_indexes(table_name)
    }


def _add_column_once(table_name: str, column: sa.Column) -> None:
    if _has_table(table_name) and not _has_column(table_name, column.name):
        op.add_column(table_name, column)


def _create_index_once(
    table_name: str,
    index_name: str,
    columns: list[str],
    *,
    unique: bool = False,
    postgresql_where=None,
) -> None:
    if _has_table(table_name) and not _has_index(table_name, index_name):
        op.create_index(
            index_name,
            table_name,
            columns,
            unique=unique,
            postgresql_where=postgresql_where,
        )


def upgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name == "sqlite":
        return

    # --- Campaigns: scheduled send window ----------------------------------
    _add_column_once("campaigns", sa.Column("send_window_start_hour", sa.Integer()))
    _add_column_once("campaigns", sa.Column("send_window_end_hour", sa.Integer()))
    _add_column_once(
        "campaigns", sa.Column("send_window_timezone", sa.String(length=64))
    )

    # --- Steps: finer-grained delay + soft disable -------------------------
    _add_column_once(
        "campaign_steps",
        sa.Column("delay_hours", sa.Integer(), nullable=False, server_default="0"),
    )
    _add_column_once(
        "campaign_steps",
        sa.Column("is_active", sa.Boolean(), nullable=False, server_default=sa.true()),
    )

    # --- Recipients: delivery tracking + unsubscribe token -----------------
    _add_column_once(
        "campaign_recipients",
        sa.Column("attempt_count", sa.Integer(), nullable=False, server_default="0"),
    )
    _add_column_once(
        "campaign_recipients", sa.Column("last_attempt_at", sa.DateTime(timezone=True))
    )
    _add_column_once(
        "campaign_recipients", sa.Column("suppressed_at", sa.DateTime(timezone=True))
    )
    _add_column_once(
        "campaign_recipients", sa.Column("unsubscribe_token", sa.String(length=64))
    )
    # Pending recipients created before this migration still need a functional
    # unsubscribe link when released afterward. Two random UUIDs provide a
    # non-guessable 256-bit token without depending on a new extension.
    op.execute(
        sa.text(
            """
            UPDATE campaign_recipients
               SET unsubscribe_token =
                   replace(gen_random_uuid()::text, '-', '') ||
                   replace(gen_random_uuid()::text, '-', '')
             WHERE unsubscribe_token IS NULL
            """
        )
    )
    _create_index_once(
        "campaign_recipients",
        "ix_campaign_recipients_step",
        ["campaign_id", "step_id"],
    )
    _create_index_once(
        "campaign_recipients",
        "uq_campaign_recipients_unsubscribe_token",
        ["unsubscribe_token"],
        unique=True,
        postgresql_where=sa.text("unsubscribe_token IS NOT NULL"),
    )


def downgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name == "sqlite":
        return

    for index_name in (
        "uq_campaign_recipients_unsubscribe_token",
        "ix_campaign_recipients_step",
    ):
        if _has_index("campaign_recipients", index_name):
            op.drop_index(index_name, table_name="campaign_recipients")
    for column_name in (
        "unsubscribe_token",
        "suppressed_at",
        "last_attempt_at",
        "attempt_count",
    ):
        if _has_column("campaign_recipients", column_name):
            op.drop_column("campaign_recipients", column_name)

    for column_name in ("is_active", "delay_hours"):
        if _has_column("campaign_steps", column_name):
            op.drop_column("campaign_steps", column_name)

    for column_name in (
        "send_window_timezone",
        "send_window_end_hour",
        "send_window_start_hour",
    ):
        if _has_column("campaigns", column_name):
            op.drop_column("campaigns", column_name)
