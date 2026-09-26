"""Add provider-confirmed email delivery states.

Revision ID: 620_zeptomail_delivery_statuses
Revises: 619_expense_approval_adjustments
Create Date: 2026-09-23
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "620_zeptomail_delivery_statuses"
down_revision: str | None = "619_expense_approval_adjustments"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute("ALTER TYPE notificationstatus ADD VALUE IF NOT EXISTS 'submitted'")
    op.execute("ALTER TYPE notificationstatus ADD VALUE IF NOT EXISTS 'bounced'")


def downgrade() -> None:
    # PostgreSQL enum values cannot be removed safely while rows may use them.
    pass
