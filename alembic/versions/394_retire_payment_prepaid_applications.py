"""Retire the unused prepaid payment-application table.

Revision ID: 394_retire_payment_prepaid_applications
Revises: 393_prepaid_coverage_reconciliation
"""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision = "394_retire_payment_prepaid_applications"
down_revision = "393_prepaid_coverage_reconciliation"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    row_exists = bool(
        bind.scalar(
            sa.text("SELECT EXISTS (SELECT 1 FROM payment_prepaid_applications)")
        )
    )
    if row_exists:
        raise RuntimeError(
            "payment_prepaid_applications contains evidence; reconcile or archive "
            "it before retiring the legacy table"
        )
    op.drop_table("payment_prepaid_applications")


def downgrade() -> None:
    # Forward-only authority retirement: downgrade must not recreate a second
    # prepaid service-consumption evidence model.
    pass
