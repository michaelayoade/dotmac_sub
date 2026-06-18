"""Add effective_date to ledger_entries.

Revision ID: 159_ledger_effective_date
Revises: 158_ip_assignments_subscription_owner
Create Date: 2026-06-18

The migrated AR ledger lost each entry's original transaction date — every row
carries the 2026-03-15 import instant in ``created_at``. This adds a nullable
``effective_date`` carrying the real-world date (invoice issue / payment date /
Splynx transaction date), populated by a separate idempotent backfill script
(scripts/billing/backfill_ledger_effective_date.py). Display and ordering use
COALESCE(effective_date, created_at), so the column is additive and safe:
NULL rows simply keep the current created_at behaviour.

Idempotent (guards on column/index existence).
"""

from __future__ import annotations

import sqlalchemy as sa
from sqlalchemy import inspect

from alembic import op

revision = "159_ledger_effective_date"
down_revision = "158_ip_assignments_subscription_owner"
branch_labels = None
depends_on = None

TABLE = "ledger_entries"
COLUMN = "effective_date"
INDEX = "ix_ledger_entries_account_id_effective_date"


def _has_column(table: str, column: str) -> bool:
    inspector = inspect(op.get_bind())
    return any(col["name"] == column for col in inspector.get_columns(table))


def _has_index(table: str, index_name: str) -> bool:
    inspector = inspect(op.get_bind())
    return any(index["name"] == index_name for index in inspector.get_indexes(table))


def upgrade() -> None:
    if not _has_column(TABLE, COLUMN):
        op.add_column(
            TABLE,
            sa.Column(COLUMN, sa.DateTime(timezone=True), nullable=True),
        )
    # Supports the per-account, date-ordered ledger/statement queries.
    if not _has_index(TABLE, INDEX):
        op.create_index(INDEX, TABLE, ["account_id", COLUMN], unique=False)


def downgrade() -> None:
    if _has_index(TABLE, INDEX):
        op.drop_index(INDEX, table_name=TABLE)
    if _has_column(TABLE, COLUMN):
        op.drop_column(TABLE, COLUMN)
