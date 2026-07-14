"""Persist the credit-note issuance tax point.

Revision ID: 291_credit_note_tax_point
Revises: 290_wht_lifecycle
"""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision = "291_credit_note_tax_point"
down_revision = "290_wht_lifecycle"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "credit_notes",
        sa.Column("issued_at", sa.DateTime(timezone=True)),
    )
    op.execute(
        sa.text(
            """
            UPDATE credit_notes
            SET issued_at = created_at
            WHERE issued_at IS NULL
              AND status IN ('issued', 'partially_applied', 'applied')
            """
        )
    )


def downgrade() -> None:
    op.drop_column("credit_notes", "issued_at")
