"""Add remaining Splynx import tables and column changes.

Alters device_metrics.value to Float, radius_accounting_sessions octets
to BigInteger, makes subscription_id/access_credential_id nullable,
adds splynx_session_id. Creates 4 archival tables for tickets and quotes.

Revision ID: t8u9v0w1x2y3
Revises: s7t8u9v0w2y3, crm_cleanup_001
Create Date: 2026-02-17 12:00:00.000000

"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "t8u9v0w1x2y3"
down_revision: tuple[str, ...] = ("s7t8u9v0w2y3", "crm_cleanup_001")
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _column_exists(inspector: sa.Inspector, table: str, column: str) -> bool:
    columns = [c["name"] for c in inspector.get_columns(table)]
    return column in columns


def _table_exists(inspector: sa.Inspector, table: str) -> bool:
    return table in inspector.get_table_names()


def _index_exists(inspector: sa.Inspector, table: str, index_name: str) -> bool:
    indexes = [idx["name"] for idx in inspector.get_indexes(table)]
    return index_name in indexes


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)

    # -- device_metrics.value: Integer → Float --
    op.alter_column(
        "device_metrics",
        "value",
        existing_type=sa.Integer(),
        type_=sa.Float(),
        existing_nullable=False,
    )

    # -- radius_accounting_sessions: octets Integer → BigInteger --
    op.alter_column(
        "radius_accounting_sessions",
        "input_octets",
        existing_type=sa.Integer(),
        type_=sa.BigInteger(),
        existing_nullable=True,
    )
    op.alter_column(
        "radius_accounting_sessions",
        "output_octets",
        existing_type=sa.Integer(),
        type_=sa.BigInteger(),
        existing_nullable=True,
    )

    # -- radius_accounting_sessions: make subscription_id nullable --
    op.alter_column(
        "radius_accounting_sessions",
        "subscription_id",
        existing_type=postgresql.UUID(as_uuid=True),
        nullable=True,
    )

    # -- radius_accounting_sessions: make access_credential_id nullable --
    op.alter_column(
        "radius_accounting_sessions",
        "access_credential_id",
        existing_type=postgresql.UUID(as_uuid=True),
        nullable=True,
    )

    # -- radius_accounting_sessions.splynx_session_id --
    if not _column_exists(inspector, "radius_accounting_sessions", "splynx_session_id"):
        op.add_column(
            "radius_accounting_sessions",
            sa.Column("splynx_session_id", sa.Integer(), nullable=True),
        )

    # -- splynx_archived_tickets --
    if not _table_exists(inspector, "splynx_archived_tickets"):
        op.create_table(
            "splynx_archived_tickets",
            sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
            sa.Column(
                "splynx_ticket_id", sa.Integer(), nullable=False, unique=True
            ),
            sa.Column(
                "subscriber_id",
                postgresql.UUID(as_uuid=True),
                sa.ForeignKey("subscribers.id"),
                nullable=True,
            ),
            sa.Column("subject", sa.String(255), nullable=False),
            sa.Column("status", sa.String(40), nullable=False, server_default="open"),
            sa.Column(
                "priority", sa.String(20), nullable=False, server_default="normal"
            ),
            sa.Column("assigned_to", sa.String(160), nullable=True),
            sa.Column("created_by", sa.String(160), nullable=True),
            sa.Column("body", sa.Text(), nullable=True),
            sa.Column(
                "splynx_metadata",
                postgresql.JSONB(),
                nullable=True,
                server_default=sa.text("'{}'::jsonb"),
            ),
            sa.Column(
                "is_active",
                sa.Boolean(),
                nullable=False,
                server_default=sa.text("true"),
            ),
            sa.Column(
                "created_at",
                sa.DateTime(timezone=True),
                nullable=False,
                server_default=sa.func.now(),
            ),
            sa.Column(
                "updated_at",
                sa.DateTime(timezone=True),
                nullable=False,
                server_default=sa.func.now(),
            ),
        )

    # -- splynx_archived_ticket_messages --
    if not _table_exists(inspector, "splynx_archived_ticket_messages"):
        op.create_table(
            "splynx_archived_ticket_messages",
            sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
            sa.Column(
                "splynx_message_id", sa.Integer(), nullable=False, unique=True
            ),
            sa.Column(
                "ticket_id",
                postgresql.UUID(as_uuid=True),
                sa.ForeignKey("splynx_archived_tickets.id"),
                nullable=False,
            ),
            sa.Column(
                "sender_type",
                sa.String(20),
                nullable=False,
                server_default="customer",
            ),
            sa.Column("sender_name", sa.String(160), nullable=True),
            sa.Column("body", sa.Text(), nullable=True),
            sa.Column(
                "is_internal",
                sa.Boolean(),
                nullable=False,
                server_default=sa.text("false"),
            ),
            sa.Column(
                "created_at",
                sa.DateTime(timezone=True),
                nullable=False,
                server_default=sa.func.now(),
            ),
        )

    # -- splynx_archived_quotes --
    if not _table_exists(inspector, "splynx_archived_quotes"):
        op.create_table(
            "splynx_archived_quotes",
            sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
            sa.Column(
                "splynx_quote_id", sa.Integer(), nullable=False, unique=True
            ),
            sa.Column(
                "subscriber_id",
                postgresql.UUID(as_uuid=True),
                sa.ForeignKey("subscribers.id"),
                nullable=True,
            ),
            sa.Column("quote_number", sa.String(60), nullable=True),
            sa.Column(
                "status", sa.String(40), nullable=False, server_default="draft"
            ),
            sa.Column("currency", sa.String(3), nullable=False, server_default="NGN"),
            sa.Column(
                "subtotal", sa.Numeric(12, 2), nullable=False, server_default="0"
            ),
            sa.Column(
                "tax_total", sa.Numeric(12, 2), nullable=False, server_default="0"
            ),
            sa.Column(
                "total", sa.Numeric(12, 2), nullable=False, server_default="0"
            ),
            sa.Column("valid_until", sa.DateTime(timezone=True), nullable=True),
            sa.Column("memo", sa.Text(), nullable=True),
            sa.Column(
                "splynx_metadata",
                postgresql.JSONB(),
                nullable=True,
                server_default=sa.text("'{}'::jsonb"),
            ),
            sa.Column(
                "is_active",
                sa.Boolean(),
                nullable=False,
                server_default=sa.text("true"),
            ),
            sa.Column(
                "created_at",
                sa.DateTime(timezone=True),
                nullable=False,
                server_default=sa.func.now(),
            ),
            sa.Column(
                "updated_at",
                sa.DateTime(timezone=True),
                nullable=False,
                server_default=sa.func.now(),
            ),
        )

    # -- splynx_archived_quote_items --
    if not _table_exists(inspector, "splynx_archived_quote_items"):
        op.create_table(
            "splynx_archived_quote_items",
            sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
            sa.Column("splynx_item_id", sa.Integer(), nullable=True),
            sa.Column(
                "quote_id",
                postgresql.UUID(as_uuid=True),
                sa.ForeignKey("splynx_archived_quotes.id"),
                nullable=False,
            ),
            sa.Column("description", sa.Text(), nullable=True),
            sa.Column(
                "quantity", sa.Numeric(10, 2), nullable=False, server_default="1"
            ),
            sa.Column(
                "unit_price", sa.Numeric(12, 2), nullable=False, server_default="0"
            ),
            sa.Column(
                "amount", sa.Numeric(12, 2), nullable=False, server_default="0"
            ),
            sa.Column(
                "created_at",
                sa.DateTime(timezone=True),
                nullable=False,
                server_default=sa.func.now(),
            ),
        )


def downgrade() -> None:
    op.drop_table("splynx_archived_quote_items")
    op.drop_table("splynx_archived_quotes")
    op.drop_table("splynx_archived_ticket_messages")
    op.drop_table("splynx_archived_tickets")

    op.drop_column("radius_accounting_sessions", "splynx_session_id")

    op.alter_column(
        "radius_accounting_sessions",
        "access_credential_id",
        existing_type=postgresql.UUID(as_uuid=True),
        nullable=False,
    )
    op.alter_column(
        "radius_accounting_sessions",
        "subscription_id",
        existing_type=postgresql.UUID(as_uuid=True),
        nullable=False,
    )
    op.alter_column(
        "radius_accounting_sessions",
        "output_octets",
        existing_type=sa.BigInteger(),
        type_=sa.Integer(),
        existing_nullable=True,
    )
    op.alter_column(
        "radius_accounting_sessions",
        "input_octets",
        existing_type=sa.BigInteger(),
        type_=sa.Integer(),
        existing_nullable=True,
    )
    op.alter_column(
        "device_metrics",
        "value",
        existing_type=sa.Float(),
        type_=sa.Integer(),
        existing_nullable=False,
    )
