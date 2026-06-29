"""Add generated invoice-line idempotency key.

Revision ID: 174_invoice_line_subscription_idempotency
Revises: 173_radius_accounting_import_lookup_index
Create Date: 2026-06-25
"""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision = "174_invoice_line_subscription_idempotency"
down_revision = "173_radius_accounting_import_lookup_index"
branch_labels = None
depends_on = None

_INDEX = "uq_invoice_lines_active_billing_line_key"
_TABLE = "invoice_lines"


def upgrade() -> None:
    op.add_column(_TABLE, sa.Column("billing_line_key", sa.String(255), nullable=True))
    bind = op.get_bind()
    if bind.dialect.name == "postgresql":
        with op.get_context().autocommit_block():
            op.execute(
                f"CREATE UNIQUE INDEX CONCURRENTLY IF NOT EXISTS {_INDEX} "
                f"ON {_TABLE} (billing_line_key) "
                "WHERE is_active AND billing_line_key IS NOT NULL"
            )
    else:
        op.create_index(
            _INDEX,
            _TABLE,
            ["billing_line_key"],
            unique=True,
            sqlite_where=sa.text("is_active AND billing_line_key IS NOT NULL"),
        )


def downgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name == "postgresql":
        with op.get_context().autocommit_block():
            op.execute(f"DROP INDEX CONCURRENTLY IF EXISTS {_INDEX}")
    else:
        op.drop_index(_INDEX, table_name=_TABLE)
    op.drop_column(_TABLE, "billing_line_key")
