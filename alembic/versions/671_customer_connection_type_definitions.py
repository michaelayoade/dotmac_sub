"""Make customer connection types configurable.

Revision ID: 671_customer_connection_type_definitions
Revises: 670_user_credential_version
Create Date: 2026-10-11
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "671_customer_connection_type_definitions"
down_revision: str | None = "670_user_credential_version"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_TABLE = "customer_connection_types"
_FK = "fk_subscribers_connection_type_definition"
_INDEX = "ix_subscribers_connection_type"


def upgrade() -> None:
    bind = op.get_bind()
    op.execute("SET LOCAL lock_timeout = '5s'")
    inspector = sa.inspect(bind)
    if not inspector.has_table(_TABLE):
        op.create_table(
            _TABLE,
            sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
            sa.Column("code", sa.String(length=64), nullable=False),
            sa.Column("name", sa.String(length=80), nullable=False),
            sa.Column("description", sa.Text()),
            sa.Column("sort_order", sa.Integer(), nullable=False, server_default="0"),
            sa.Column(
                "is_active", sa.Boolean(), nullable=False, server_default=sa.true()
            ),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
            sa.PrimaryKeyConstraint("id"),
            sa.UniqueConstraint("code", name="uq_customer_connection_types_code"),
            sa.UniqueConstraint("name", name="uq_customer_connection_types_name"),
        )

    op.execute(
        sa.text(
            """
            INSERT INTO customer_connection_types
                (id, code, name, description, sort_order, is_active, created_at, updated_at)
            VALUES
                ('00000000-0000-0000-0000-000000000101', 'wireless', 'Wireless',
                 'Radio or other wireless last-mile connection.', 10, true, now(), now()),
                ('00000000-0000-0000-0000-000000000102', 'wired', 'Wired',
                 'Fibre, copper, or cable last-mile connection.', 20, true, now(), now())
            ON CONFLICT (code) DO NOTHING
            """
        )
    )

    op.execute(
        "ALTER TABLE subscribers DROP CONSTRAINT IF EXISTS "
        "ck_subscribers_connection_type"
    )
    subscriber_columns = {
        column["name"]: column for column in inspector.get_columns("subscribers")
    }
    connection_column = subscriber_columns.get("connection_type")
    if (
        connection_column is not None
        and getattr(connection_column["type"], "length", None) != 64
    ):
        op.alter_column(
            "subscribers",
            "connection_type",
            existing_type=sa.String(length=16),
            type_=sa.String(length=64),
            existing_nullable=True,
        )

    inspector = sa.inspect(bind)
    foreign_keys = {
        item.get("name") for item in inspector.get_foreign_keys("subscribers")
    }
    if _FK not in foreign_keys:
        op.create_foreign_key(
            _FK,
            "subscribers",
            _TABLE,
            ["connection_type"],
            ["code"],
            ondelete="RESTRICT",
        )
    indexes = {item.get("name") for item in inspector.get_indexes("subscribers")}
    if _INDEX not in indexes:
        op.create_index(_INDEX, "subscribers", ["connection_type"])


def downgrade() -> None:
    op.execute("SET LOCAL lock_timeout = '5s'")
    op.execute(
        "UPDATE subscribers SET connection_type = NULL "
        "WHERE connection_type NOT IN ('wireless', 'wired')"
    )
    op.execute(f"ALTER TABLE subscribers DROP CONSTRAINT IF EXISTS {_FK}")
    op.execute(f"DROP INDEX IF EXISTS {_INDEX}")
    op.alter_column(
        "subscribers",
        "connection_type",
        existing_type=sa.String(length=64),
        type_=sa.String(length=16),
        existing_nullable=True,
    )
    op.create_check_constraint(
        "ck_subscribers_connection_type",
        "subscribers",
        "connection_type IS NULL OR connection_type IN ('wireless', 'wired')",
    )
    op.drop_table(_TABLE)
