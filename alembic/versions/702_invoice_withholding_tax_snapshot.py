"""Snapshot withholding tax on issued customer invoices.

Revision ID: 702_invoice_withholding_tax_snapshot
Revises: 701_olt_ssh_host_key_record
Create Date: 2026-10-28
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "702_invoice_withholding_tax_snapshot"
down_revision: str | None = "701_olt_ssh_host_key_record"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_TABLE = "invoices"
_CHECK = "ck_invoices_withholding_tax_snapshot"


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    columns = {column["name"] for column in inspector.get_columns(_TABLE)}
    additions = (
        sa.Column("withholding_tax_rate_percent", sa.Numeric(6, 2), nullable=True),
        sa.Column("withholding_tax_amount", sa.Numeric(12, 2), nullable=True),
        sa.Column("withholding_tax_policy_version", sa.Integer(), nullable=True),
        sa.Column("net_amount_payable", sa.Numeric(12, 2), nullable=True),
    )
    for column in additions:
        if column.name not in columns:
            op.add_column(_TABLE, column)

    checks = {check["name"] for check in sa.inspect(bind).get_check_constraints(_TABLE)}
    if _CHECK not in checks:
        op.create_check_constraint(
            _CHECK,
            _TABLE,
            "(withholding_tax_rate_percent IS NULL AND "
            "withholding_tax_amount IS NULL AND "
            "withholding_tax_policy_version IS NULL AND "
            "net_amount_payable IS NULL) OR "
            "(withholding_tax_rate_percent > 0 AND "
            "withholding_tax_rate_percent < 100 AND "
            "withholding_tax_amount > 0 AND "
            "withholding_tax_policy_version > 0 AND "
            "net_amount_payable > 0 AND "
            "net_amount_payable + withholding_tax_amount = total)",
        )


def downgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    checks = {check["name"] for check in inspector.get_check_constraints(_TABLE)}
    if _CHECK in checks:
        op.drop_constraint(_CHECK, _TABLE, type_="check")
    columns = {column["name"] for column in inspector.get_columns(_TABLE)}
    for name in (
        "net_amount_payable",
        "withholding_tax_policy_version",
        "withholding_tax_amount",
        "withholding_tax_rate_percent",
    ):
        if name in columns:
            op.drop_column(_TABLE, name)
