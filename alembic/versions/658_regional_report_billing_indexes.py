"""Add billing date indexes used by regional reporting.

The indexes are built concurrently on PostgreSQL so report-read remediation
does not block invoice or payment writes. The migration remains idempotent for
rehearsals and safe to retry after a statement-timeout or deploy interruption.
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op
from scripts.migration.regional_report_billing_indexes import (
    INDEX_SPECS,
    ensure_postgres_indexes,
)

revision: str = "658_regional_report_billing_indexes"
down_revision: tuple[str, str] = (
    "653_prepaid_activation_funding_overrides",
    "657_purchase_prepaid_sweep_merge",
)
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Make bounded invoice/payment period scans use their reporting keys."""

    bind = op.get_bind()
    if bind.dialect.name == "postgresql":
        with op.get_context().autocommit_block():
            ensure_postgres_indexes(bind, op.execute)
        return
    for spec in INDEX_SPECS:
        op.execute(
            f"CREATE INDEX IF NOT EXISTS {spec.name} "
            f"ON {spec.table} ({', '.join(spec.keys)})"
        )


def downgrade() -> None:
    bind = op.get_bind()
    concurrently = " CONCURRENTLY" if bind.dialect.name == "postgresql" else ""
    statements = (
        f"DROP INDEX{concurrently} IF EXISTS ix_payments_regional_report_period",
        f"DROP INDEX{concurrently} IF EXISTS ix_invoices_regional_report_period",
    )
    if bind.dialect.name == "postgresql":
        with op.get_context().autocommit_block():
            for statement in statements:
                op.execute(statement)
        return
    for statement in statements:
        op.execute(statement)
