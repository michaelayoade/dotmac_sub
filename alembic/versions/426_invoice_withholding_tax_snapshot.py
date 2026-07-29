"""Persist immutable customer-WHT invoice snapshots.

Revision ID: 426_invoice_withholding_tax_snapshot
Revises: 418_customer_wht_policy_and_direct_targets
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "426_invoice_withholding_tax_snapshot"
down_revision = "418_customer_wht_policy_and_direct_targets"
branch_labels = None
depends_on = None


def upgrade() -> None:
    columns = (
        sa.Column("withholding_tax_rate", sa.Numeric(5, 2)),
        sa.Column("withholding_tax_rate_provenance", sa.String(80)),
        sa.Column(
            "withholding_tax_amount",
            sa.Numeric(12, 2),
            nullable=False,
            server_default=sa.text("0"),
        ),
        sa.Column("withholding_tax_taxable_basis", sa.Numeric(12, 2)),
        sa.Column("bank_transfer_net_payable", sa.Numeric(12, 2)),
        sa.Column("withholding_tax_policy_enabled", sa.Boolean()),
        sa.Column("withholding_tax_policy_version", sa.Integer()),
    )
    constraints = (
        ("ck_invoices_wht_snapshot_basis_present", "withholding_tax_policy_enabled IS NULL OR withholding_tax_taxable_basis IS NOT NULL"),
        ("ck_invoices_wht_snapshot_net_payable_present", "withholding_tax_policy_enabled IS NULL OR bank_transfer_net_payable IS NOT NULL"),
        ("ck_invoices_wht_snapshot_rate_range", "withholding_tax_rate IS NULL OR (withholding_tax_rate > 0 AND withholding_tax_rate < 100)"),
        ("ck_invoices_wht_snapshot_amount_nonnegative", "withholding_tax_amount >= 0"),
    )
    if op.get_bind().dialect.name == "sqlite":
        with op.batch_alter_table("invoices", recreate="always") as batch:
            for column in columns:
                batch.add_column(column)
            for name, expression in constraints:
                batch.create_check_constraint(name, expression)
        return
    for column in columns:
        op.add_column("invoices", column)
    for name, expression in constraints:
        op.create_check_constraint(name, "invoices", expression)


def _validate_downgrade() -> None:
    rows = op.get_bind().execute(
        sa.text(
            "SELECT id FROM invoices "
            "WHERE withholding_tax_policy_enabled IS NOT NULL LIMIT 1"
        )
    ).fetchall()
    if rows:
        raise RuntimeError(
            "cannot downgrade while immutable invoice withholding-tax snapshots exist"
        )


def downgrade() -> None:
    constraints = (
        "ck_invoices_wht_snapshot_amount_nonnegative",
        "ck_invoices_wht_snapshot_rate_range",
        "ck_invoices_wht_snapshot_net_payable_present",
        "ck_invoices_wht_snapshot_basis_present",
    )
    columns = (
        "withholding_tax_policy_version",
        "withholding_tax_policy_enabled",
        "bank_transfer_net_payable",
        "withholding_tax_taxable_basis",
        "withholding_tax_amount",
        "withholding_tax_rate_provenance",
        "withholding_tax_rate",
    )
    _validate_downgrade()
    if op.get_bind().dialect.name == "sqlite":
        with op.batch_alter_table("invoices", recreate="always") as batch:
            for name in constraints:
                batch.drop_constraint(name, type_="check")
            for column in columns:
                batch.drop_column(column)
        return
    for name in constraints:
        op.drop_constraint(name, "invoices")
    for column in columns:
        op.drop_column("invoices", column)
