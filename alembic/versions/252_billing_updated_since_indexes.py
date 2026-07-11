"""Performance: indexes backing the billing incremental-sync watermark.

Incident: the ERP AR sync (dotmac_erp) re-listed *every* invoice each cycle via
OFFSET pagination ordered by ``created_at``. No index led on ``created_at``, so
each page did a global sequential sort; under the deep offsets that produced
long-running DB sessions that starved dotmac_sub's app connection pool
(QueuePool timeouts).

The durable fix is incremental sync via an ``updated_since`` watermark on
``GET /invoices`` (+ ``/payments`` + ``/credit-notes``, which share the pattern).
``updated_at`` already exists on all three tables (server-tracked via the ORM
``onupdate``), so this migration only adds the supporting indexes:

- ix_<table>_is_active_updated_at — backs ``WHERE is_active AND updated_at >=
  :cutoff ORDER BY updated_at`` (the watermark / sync path).
- ix_<table>_is_active_created_at — backs the un-watermarked UI default
  (``ORDER BY created_at DESC`` over active rows), so it stops seq-sorting too.

On Postgres the indexes build CONCURRENTLY (outside a transaction) so the build
never locks writes on these hot money tables. All statements are IF NOT EXISTS,
so this is idempotent and a no-op where the model-level Index() already created
them (fresh create_all in the SQLite test suite).

Revision ID: 252_billing_updated_since_indexes
Revises: 251_native_read_path_indexes
Create Date: 2026-07-11
"""

from __future__ import annotations

from alembic import op

revision = "252_billing_updated_since_indexes"
down_revision = "251_native_read_path_indexes"
branch_labels = None
depends_on = None

# (index_name, table, "col, col, ...")
_INDEXES: list[tuple[str, str, str]] = [
    ("ix_invoices_is_active_updated_at", "invoices", "is_active, updated_at"),
    ("ix_invoices_is_active_created_at", "invoices", "is_active, created_at"),
    ("ix_payments_is_active_updated_at", "payments", "is_active, updated_at"),
    ("ix_payments_is_active_created_at", "payments", "is_active, created_at"),
    (
        "ix_credit_notes_is_active_updated_at",
        "credit_notes",
        "is_active, updated_at",
    ),
    (
        "ix_credit_notes_is_active_created_at",
        "credit_notes",
        "is_active, created_at",
    ),
]


def upgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name == "postgresql":
        # CONCURRENTLY cannot run inside a transaction block.
        with op.get_context().autocommit_block():
            for name, table, cols in _INDEXES:
                op.execute(
                    f"CREATE INDEX CONCURRENTLY IF NOT EXISTS {name} "
                    f"ON {table} ({cols})"
                )
    else:
        for name, table, cols in _INDEXES:
            op.execute(f"CREATE INDEX IF NOT EXISTS {name} ON {table} ({cols})")


def downgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name == "postgresql":
        with op.get_context().autocommit_block():
            for name, _table, _cols in _INDEXES:
                op.execute(f"DROP INDEX CONCURRENTLY IF EXISTS {name}")
    else:
        for name, _table, _cols in _INDEXES:
            op.execute(f"DROP INDEX IF EXISTS {name}")
