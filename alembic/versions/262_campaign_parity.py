"""Campaign parity: SMTP configs, suppression list, step + delivery tracking.

Revision ID: 262_campaign_parity
Revises: 261_system_user_role_source
Create Date: 2026-07-12
"""

from __future__ import annotations

import sqlalchemy as sa
from sqlalchemy import inspect
from sqlalchemy.dialects import postgresql

from alembic import op

revision = "262_campaign_parity"
down_revision = "261_system_user_role_source"
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


def _json_type() -> sa.types.TypeEngine:
    if op.get_bind().dialect.name == "postgresql":
        return postgresql.JSONB(astext_type=sa.Text())
    return sa.JSON()


def _uuid_type() -> sa.types.TypeEngine:
    if op.get_bind().dialect.name == "postgresql":
        return postgresql.UUID(as_uuid=True)
    return sa.String(length=36)


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

    uuid_type = _uuid_type()
    json_type = _json_type()

    # --- Named SMTP relays for campaign mail -------------------------------
    if not _has_table("campaign_smtp_configs"):
        op.create_table(
            "campaign_smtp_configs",
            sa.Column("id", uuid_type, primary_key=True),
            sa.Column("name", sa.String(length=160), nullable=False),
            sa.Column("host", sa.String(length=255), nullable=False),
            sa.Column("port", sa.Integer(), nullable=False, server_default="587"),
            sa.Column("username", sa.String(length=255)),
            sa.Column("password", sa.String(length=255)),
            sa.Column(
                "use_tls", sa.Boolean(), nullable=False, server_default=sa.true()
            ),
            sa.Column(
                "use_ssl", sa.Boolean(), nullable=False, server_default=sa.false()
            ),
            sa.Column(
                "is_active", sa.Boolean(), nullable=False, server_default=sa.true()
            ),
            sa.Column("metadata", json_type),
            sa.Column("created_at", sa.DateTime(timezone=True)),
            sa.Column("updated_at", sa.DateTime(timezone=True)),
            sa.UniqueConstraint("name", name="uq_campaign_smtp_configs_name"),
        )
    _create_index_once(
        "campaign_smtp_configs",
        "ix_campaign_smtp_configs_active",
        ["is_active", "name"],
    )

    # --- Sender profiles can point at a relay ------------------------------
    _add_column_once(
        "campaign_senders",
        sa.Column(
            "campaign_smtp_config_id",
            uuid_type,
            sa.ForeignKey("campaign_smtp_configs.id"),
        ),
    )

    # --- Campaigns: relay + scheduled send window --------------------------
    _add_column_once(
        "campaigns",
        sa.Column(
            "campaign_smtp_config_id",
            uuid_type,
            sa.ForeignKey("campaign_smtp_configs.id"),
        ),
    )
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

    # --- Suppression list --------------------------------------------------
    if not _has_table("campaign_suppressions"):
        op.create_table(
            "campaign_suppressions",
            sa.Column("id", uuid_type, primary_key=True),
            sa.Column("channel", sa.String(length=40), nullable=False),
            sa.Column("address", sa.String(length=255), nullable=False),
            sa.Column("subscriber_id", uuid_type, sa.ForeignKey("subscribers.id")),
            sa.Column("campaign_id", uuid_type, sa.ForeignKey("campaigns.id")),
            sa.Column(
                "reason",
                sa.String(length=40),
                nullable=False,
                server_default="unsubscribed",
            ),
            sa.Column("source", sa.String(length=80)),
            sa.Column("notes", sa.Text()),
            sa.Column("metadata", json_type),
            sa.Column("created_at", sa.DateTime(timezone=True)),
            sa.Column("updated_at", sa.DateTime(timezone=True)),
            sa.UniqueConstraint(
                "channel", "address", name="uq_campaign_suppressions_address"
            ),
        )
    _create_index_once(
        "campaign_suppressions",
        "ix_campaign_suppressions_subscriber",
        ["subscriber_id"],
    )


def downgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name == "sqlite":
        return

    if _has_table("campaign_suppressions"):
        op.drop_table("campaign_suppressions")

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
        "campaign_smtp_config_id",
    ):
        if _has_column("campaigns", column_name):
            op.drop_column("campaigns", column_name)

    if _has_column("campaign_senders", "campaign_smtp_config_id"):
        op.drop_column("campaign_senders", "campaign_smtp_config_id")

    if _has_index("campaign_smtp_configs", "ix_campaign_smtp_configs_active"):
        op.drop_index(
            "ix_campaign_smtp_configs_active", table_name="campaign_smtp_configs"
        )
    if _has_table("campaign_smtp_configs"):
        op.drop_table("campaign_smtp_configs")
