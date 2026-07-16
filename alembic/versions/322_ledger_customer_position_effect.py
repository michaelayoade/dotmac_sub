"""Classify ledger rows that affect the customer financial position.

Revision ID: 322_ledger_customer_position_effect
Revises: 321_prepaid_funding_reconstruction
"""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision = "322_ledger_customer_position_effect"
down_revision = "321_prepaid_funding_reconstruction"
branch_labels = None
depends_on = None

_STRUCTURAL_EXACT = ("Prepaid opening balance @ cutover",)
_STRUCTURAL_PREFIXES = (
    "Correction:",
    "Partial cutover opening balance construction adjustment",
    "Reversal of phantom",
    "Reversal of prepaid opening",
    "Data repair 2026-06-29:",
    "Validated account credit consumed",
    "Payment refund account-credit consumption:",
    "Payment reversal account-credit consumption:",
    "Payment allocation account-credit consumption:",
)


def upgrade() -> None:
    op.add_column(
        "ledger_entries",
        sa.Column(
            "affects_customer_position",
            sa.Boolean(),
            nullable=False,
            server_default=sa.true(),
        ),
    )
    ledger_entries = sa.table(
        "ledger_entries",
        sa.column("memo", sa.Text()),
        sa.column("affects_customer_position", sa.Boolean()),
    )
    predicate = ledger_entries.c.memo.in_(_STRUCTURAL_EXACT)
    for prefix in _STRUCTURAL_PREFIXES:
        predicate = sa.or_(predicate, ledger_entries.c.memo.like(f"{prefix}%"))
    op.execute(
        ledger_entries.update().where(predicate).values(affects_customer_position=False)
    )
    op.alter_column(
        "ledger_entries",
        "affects_customer_position",
        server_default=None,
    )


def downgrade() -> None:
    op.drop_column("ledger_entries", "affects_customer_position")
