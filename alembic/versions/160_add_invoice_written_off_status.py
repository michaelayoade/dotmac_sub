"""Add 'written_off' value to invoicestatus enum.

Bad-debt write-offs become a dedicated ``written_off`` invoice status —
closed-but-not-collected — instead of being forced to ``void`` (which means
"never existed" and vanishes from AR/aging) or ``paid`` (which would count the
loss as cash). The financial loss remains recorded as a credit adjustment in
the ledger (the source of truth); this is purely a status classification.

Revision ID: 160_add_invoice_written_off_status
Revises: 159_ledger_effective_date
Create Date: 2026-06-19

Note: historical write-offs already stored as ``void`` are NOT reclassified
here — ``void`` was used for both genuine voids and write-offs and they are not
reliably distinguishable (free-text memo). Reclassification, if wanted, is a
separate, manually-scoped data migration.
"""

from __future__ import annotations

from alembic import op

revision = "160_add_invoice_written_off_status"
down_revision = "159_ledger_effective_date"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name != "postgresql":
        # SQLite (tests) stores the enum as a string/CHECK derived from the
        # model, so the new member is available without a DDL change.
        return
    # Idempotent + order-independent; safe under the repo's multi-head state.
    op.execute(
        "ALTER TYPE invoicestatus ADD VALUE IF NOT EXISTS 'written_off' AFTER 'void'"
    )


def downgrade() -> None:
    # PostgreSQL cannot drop an enum value without a full type rebuild, which is
    # disruptive in production. Leave the value in place.
    pass
