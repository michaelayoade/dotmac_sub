"""Retire the competing prepaid monthly invoice control.

Revision ID: 392_retire_prepaid_monthly_invoice_owner
Revises: 391_payment_receipt_notification
"""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision = "392_retire_prepaid_monthly_invoice_owner"
down_revision = "391_payment_receipt_notification"
branch_labels = None
depends_on = None

_DELETE_SETTING = sa.text(
    "DELETE FROM domain_settings WHERE domain = CAST(:domain AS settingdomain) "
    "AND key = :key"
)


def upgrade() -> None:
    # The module key is the canonical feature-control persistence identity; the
    # billing key is its former environment/settings alias. Neither may survive
    # as a second writer for prepaid service periods.
    op.execute(
        _DELETE_SETTING.bindparams(
            domain="modules", key="billing_prepaid_monthly_invoicing"
        )
    )
    op.execute(
        _DELETE_SETTING.bindparams(
            domain="billing", key="prepaid_monthly_invoicing_enabled"
        )
    )


def downgrade() -> None:
    # Forward-only authority cutover: downgrade must not recreate a competing
    # service-period writer.
    pass
