"""Add bounded-candidate indexes for the Upcoming Charges worklist.

Revision ID: 559_upcoming_charges_indexes
Revises: 558_receivable_projection
Create Date: 2026-08-26

The indexes match the report's first-stage time-window scans and latest-period
lookup. PostgreSQL builds them concurrently so invoice and entitlement writes
remain available.
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "559_upcoming_charges_indexes"
down_revision: str | None = "558_receivable_projection"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_INVOICE_INDEX = "ix_invoices_upcoming_collectible_due"
_ENTITLEMENT_INDEX = "ix_service_entitlements_active_end_subscription"
_LATEST_ENTITLEMENT_INDEX = "ix_service_entitlements_active_subscription_end"


def upgrade() -> None:
    bind = op.get_bind()
    concurrently = " CONCURRENTLY" if bind.dialect.name == "postgresql" else ""
    statements = (
        f"CREATE INDEX{concurrently} IF NOT EXISTS {_INVOICE_INDEX} "
        "ON invoices (due_at, account_id) "
        "WHERE is_active AND balance_due > 0 AND due_at IS NOT NULL "
        "AND status IN ('issued', 'partially_paid', 'overdue')",
        f"CREATE INDEX{concurrently} IF NOT EXISTS {_ENTITLEMENT_INDEX} "
        "ON service_entitlements (ends_at, subscription_id) "
        "WHERE status = 'active'",
        f"CREATE INDEX{concurrently} IF NOT EXISTS {_LATEST_ENTITLEMENT_INDEX} "
        "ON service_entitlements (subscription_id, ends_at) "
        "WHERE status = 'active'",
    )
    if bind.dialect.name == "postgresql":
        with op.get_context().autocommit_block():
            for statement in statements:
                op.execute(statement)
        return
    for statement in statements:
        op.execute(statement)


def downgrade() -> None:
    bind = op.get_bind()
    concurrently = " CONCURRENTLY" if bind.dialect.name == "postgresql" else ""
    statements = (
        f"DROP INDEX{concurrently} IF EXISTS {_LATEST_ENTITLEMENT_INDEX}",
        f"DROP INDEX{concurrently} IF EXISTS {_ENTITLEMENT_INDEX}",
        f"DROP INDEX{concurrently} IF EXISTS {_INVOICE_INDEX}",
    )
    if bind.dialect.name == "postgresql":
        with op.get_context().autocommit_block():
            for statement in statements:
                op.execute(statement)
        return
    for statement in statements:
        op.execute(statement)
