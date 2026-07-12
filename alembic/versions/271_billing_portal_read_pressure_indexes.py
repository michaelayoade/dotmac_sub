"""Reduce customer billing portal read pressure.

The customer billing page builds the customer financial ledger from several
read-only queries. During pool pressure we observed idle transactions sitting on
``splynx_billing_transactions`` and ``payment_allocations`` reads. These
composite indexes back the exact predicates used by that page so each request
spends less time holding a pooled connection.

Revision ID: 271_billing_portal_read_pressure_indexes
Revises: 270_network_operation_router_types
"""

from __future__ import annotations

from alembic import op

revision = "271_billing_portal_read_pressure_indexes"
down_revision = "270_network_operation_router_types"
branch_labels = None
depends_on = None

_INDEXES: list[tuple[str, str, str]] = [
    (
        "ix_splynx_billing_transactions_subscriber_deleted_date",
        "splynx_billing_transactions",
        "subscriber_id, deleted, transaction_date",
    ),
    (
        "ix_payment_allocations_invoice_active_payment",
        "payment_allocations",
        "invoice_id, is_active, payment_id",
    ),
]


def upgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name == "postgresql":
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
