"""Ensure the retired prepaid payment-application archive exists.

Revision ID: 396_payment_prepaid_application_archive
Revises: 395_provider_event_provenance

Revision 394 now renames the legacy table so every row survives unchanged.
This compatibility revision creates the same empty archive shape only for a
database that had already applied the earlier empty-table-only form of revision
394 before that correction.  A database with evidence could not have passed the
earlier fail-closed gate, so this branch cannot conceal deleted rows.

The archive has no application model or writer.  Finance operations owns its
retention, and physical deletion requires a separate reviewed decision.
"""

from __future__ import annotations

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision = "396_payment_prepaid_application_archive"
down_revision = "395_provider_event_provenance"
branch_labels = None
depends_on = None

_ARCHIVE_TABLE = "payment_prepaid_applications_archive"
_LEGACY_TABLE = "payment_prepaid_applications"


def _has_table(bind, table_name: str) -> bool:
    return sa.inspect(bind).has_table(table_name)


def upgrade() -> None:
    bind = op.get_bind()
    legacy_exists = _has_table(bind, _LEGACY_TABLE)
    archive_exists = _has_table(bind, _ARCHIVE_TABLE)
    if legacy_exists:
        archive_state = "also exists" if archive_exists else "is missing"
        raise RuntimeError(
            "prepaid payment-application archive compatibility is ambiguous: "
            f"{_LEGACY_TABLE} still exists and {_ARCHIVE_TABLE} {archive_state}; "
            "revision 394 must retire the legacy table first"
        )
    if archive_exists:
        return

    op.create_table(
        _ARCHIVE_TABLE,
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("payment_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("settlement_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("account_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("subscription_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column(
            "credit_ledger_entry_id", postgresql.UUID(as_uuid=True), nullable=False
        ),
        sa.Column(
            "debit_ledger_entry_id", postgresql.UUID(as_uuid=True), nullable=False
        ),
        sa.Column("entitlement_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column(
            "retired_allocation_id", postgresql.UUID(as_uuid=True), nullable=True
        ),
        sa.Column(
            "historical_invoice_id", postgresql.UUID(as_uuid=True), nullable=True
        ),
        sa.Column("invoice_closure_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("origin", sa.String(length=32), nullable=False),
        sa.Column("amount", sa.Numeric(precision=12, scale=2), nullable=False),
        sa.Column("currency", sa.String(length=3), nullable=False),
        sa.Column("period_start", sa.DateTime(timezone=True), nullable=False),
        sa.Column("period_end", sa.DateTime(timezone=True), nullable=False),
        sa.Column("reason", sa.Text(), nullable=False),
        sa.Column("preview_fingerprint", sa.String(length=64), nullable=False),
        sa.Column("idempotency_key", sa.String(length=120), nullable=False),
        sa.Column(
            "access_recheck_status",
            sa.String(length=24),
            nullable=False,
            server_default="not_required",
        ),
        sa.Column("access_recheck_error", sa.String(length=120), nullable=True),
        sa.Column("access_rechecked_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("CURRENT_TIMESTAMP"),
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("CURRENT_TIMESTAMP"),
        ),
        sa.CheckConstraint(
            "amount > 0", name="ck_payment_prepaid_applications_amount_positive"
        ),
        sa.CheckConstraint(
            "period_end > period_start",
            name="ck_payment_prepaid_applications_period_order",
        ),
        sa.CheckConstraint(
            "origin IN ('historical_reconciliation', 'post_settlement')",
            name="ck_payment_prepaid_applications_origin",
        ),
        sa.CheckConstraint(
            "access_recheck_status IN "
            "('not_required', 'pending', 'completed', 'deferred')",
            name="ck_payment_prepaid_applications_access_status",
        ),
        sa.ForeignKeyConstraint(["payment_id"], ["payments.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(
            ["settlement_id"], ["payment_settlements.id"], ondelete="RESTRICT"
        ),
        sa.ForeignKeyConstraint(
            ["account_id"], ["subscribers.id"], ondelete="RESTRICT"
        ),
        sa.ForeignKeyConstraint(
            ["subscription_id"], ["subscriptions.id"], ondelete="RESTRICT"
        ),
        sa.ForeignKeyConstraint(
            ["credit_ledger_entry_id"], ["ledger_entries.id"], ondelete="RESTRICT"
        ),
        sa.ForeignKeyConstraint(
            ["debit_ledger_entry_id"], ["ledger_entries.id"], ondelete="RESTRICT"
        ),
        sa.ForeignKeyConstraint(
            ["entitlement_id"], ["service_entitlements.id"], ondelete="RESTRICT"
        ),
        sa.ForeignKeyConstraint(
            ["retired_allocation_id"],
            ["payment_allocations.id"],
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["historical_invoice_id"], ["invoices.id"], ondelete="RESTRICT"
        ),
        sa.ForeignKeyConstraint(
            ["invoice_closure_id"], ["invoice_closures.id"], ondelete="RESTRICT"
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    for column in (
        "payment_id",
        "settlement_id",
        "credit_ledger_entry_id",
        "debit_ledger_entry_id",
        "entitlement_id",
        "retired_allocation_id",
        "invoice_closure_id",
        "idempotency_key",
    ):
        op.create_index(
            f"uq_payment_prepaid_applications_{column}",
            _ARCHIVE_TABLE,
            [column],
            unique=True,
        )


def downgrade() -> None:
    # Forward-only evidence retention: never drop the archive on downgrade.
    pass
