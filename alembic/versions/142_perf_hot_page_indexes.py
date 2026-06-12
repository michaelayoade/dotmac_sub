"""Performance: add missing FK / composite indexes for hot pages.

Postgres does not auto-index foreign keys. The customer dashboard, billing,
and usage pages (plus the /me/* APIs) filter and join on these columns on
every render; without indexes each render sequentially scans the table, which
multiplies badly under concurrency. Adds:

- invoices(account_id, is_active, issued_at) — per-account billing list + FK join
- subscriptions(subscriber_id, status)
- radius_accounting_sessions(subscription_id)
- quota_buckets(subscription_id, period_start)
- usage_records(subscription_id, recorded_at)
- invoice_lines(invoice_id)
- payment_allocations(invoice_id) — UC index is payment_id-leading

On Postgres the indexes are built CONCURRENTLY (outside a transaction) so the
build does not lock writes on the larger tables (radius_accounting_sessions,
usage_records). All statements are IF NOT EXISTS so this is idempotent and a
no-op where the model-level Index() already created them (fresh create_all).

Revision ID: 142_perf_hot_page_indexes
Revises: 141_billing_money_hardening
Create Date: 2026-06-12
"""

from __future__ import annotations

from alembic import op

revision = "142_perf_hot_page_indexes"
down_revision = "141_billing_money_hardening"
branch_labels = None
depends_on = None

# (index_name, table, "col, col, ...")
_INDEXES: list[tuple[str, str, str]] = [
    (
        "ix_invoices_account_id_is_active_issued_at",
        "invoices",
        "account_id, is_active, issued_at",
    ),
    (
        "ix_subscriptions_subscriber_id_status",
        "subscriptions",
        "subscriber_id, status",
    ),
    (
        "ix_radius_accounting_sessions_subscription_id",
        "radius_accounting_sessions",
        "subscription_id",
    ),
    (
        "ix_quota_buckets_subscription_id_period_start",
        "quota_buckets",
        "subscription_id, period_start",
    ),
    (
        "ix_usage_records_subscription_id_recorded_at",
        "usage_records",
        "subscription_id, recorded_at",
    ),
    ("ix_invoice_lines_invoice_id", "invoice_lines", "invoice_id"),
    ("ix_payment_allocations_invoice_id", "payment_allocations", "invoice_id"),
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
