"""Add billing date indexes used by regional reporting.

The indexes are built concurrently on PostgreSQL so report-read remediation
does not block invoice or payment writes. The migration remains idempotent for
rehearsals and safe to retry after a statement-timeout or deploy interruption.
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "653_regional_report_billing_indexes"
down_revision: str | None = "652_renewal_terms_record_permission"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Make bounded invoice/payment period scans use their reporting keys."""

    bind = op.get_bind()
    concurrently = " CONCURRENTLY" if bind.dialect.name == "postgresql" else ""
    statements = (
        "CREATE INDEX{concurrently} IF NOT EXISTS "
        "ix_invoices_regional_report_period ON invoices "
        "(is_active, status, issued_at, account_id)",
        "CREATE INDEX{concurrently} IF NOT EXISTS "
        "ix_payments_regional_report_period ON payments "
        "(is_active, status, paid_at, account_id)",
    )
    rendered = tuple(
        statement.format(concurrently=concurrently) for statement in statements
    )
    if bind.dialect.name == "postgresql":
        with op.get_context().autocommit_block():
            for statement in rendered:
                op.execute(statement)
        return
    for statement in rendered:
        op.execute(statement)


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
