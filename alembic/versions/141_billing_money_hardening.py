"""Billing & money hardening: proof amount override, arrangement approver,
autopay failure tracking.

- payment_proofs.verified_amount: the admin-confirmed amount the Payment is
  created for (claimed amount stays on `amount` for audit).
- payment_arrangements.approved_by_user_id: SystemUser id of the approving
  admin (plain string — the existing approver FK points at subscribers and
  cannot hold staff users).
- autopay_mandates.failure_count / last_failure_at / last_failure_reason:
  consecutive-decline tracking; mandates at the cap are skipped by the charge
  engine until re-enabled or a new default card is set.

Revision ID: 141_billing_money_hardening
Revises: 140_merge_live_and_main_mfa_heads
Create Date: 2026-06-11
"""

from __future__ import annotations

import sqlalchemy as sa
from sqlalchemy import inspect

from alembic import op

revision = "141_billing_money_hardening"
down_revision = "140_merge_live_and_main_mfa_heads"
branch_labels = None
depends_on = None

_COLUMNS: dict[str, list[sa.Column]] = {
    "payment_proofs": [
        sa.Column("verified_amount", sa.Numeric(12, 2), nullable=True),
    ],
    "payment_arrangements": [
        sa.Column("approved_by_user_id", sa.String(36), nullable=True),
    ],
    "autopay_mandates": [
        sa.Column("failure_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("last_failure_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_failure_reason", sa.String(255), nullable=True),
    ],
}


def upgrade() -> None:
    bind = op.get_bind()
    inspector = inspect(bind)
    tables = set(inspector.get_table_names())
    for table, columns in _COLUMNS.items():
        if table not in tables:
            continue
        existing = {c["name"] for c in inspector.get_columns(table)}
        for column in columns:
            if column.name not in existing:
                op.add_column(table, column)


def downgrade() -> None:
    bind = op.get_bind()
    inspector = inspect(bind)
    tables = set(inspector.get_table_names())
    for table, columns in _COLUMNS.items():
        if table not in tables:
            continue
        existing = {c["name"] for c in inspector.get_columns(table)}
        for column in columns:
            if column.name in existing:
                op.drop_column(table, column.name)
