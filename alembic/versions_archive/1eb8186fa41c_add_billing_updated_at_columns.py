"""add_billing_updated_at_columns

Revision ID: 1eb8186fa41c
Revises: j1k2l3m4n5o6
Create Date: 2026-03-09 18:00:00.000000
"""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision = "1eb8186fa41c"
down_revision = "j1k2l3m4n5o6"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Add updated_at to ledger_entries (idempotent)
    conn = op.get_bind()
    inspector = sa.inspect(conn)
    ledger_columns = [c["name"] for c in inspector.get_columns("ledger_entries")]
    if "updated_at" not in ledger_columns:
        op.add_column(
            "ledger_entries",
            sa.Column(
                "updated_at",
                sa.DateTime(timezone=True),
                server_default=sa.func.now(),
                nullable=True,
            ),
        )

    # Add updated_at to credit_note_applications (idempotent)
    cna_columns = [c["name"] for c in inspector.get_columns("credit_note_applications")]
    if "updated_at" not in cna_columns:
        op.add_column(
            "credit_note_applications",
            sa.Column(
                "updated_at",
                sa.DateTime(timezone=True),
                server_default=sa.func.now(),
                nullable=True,
            ),
        )


def downgrade() -> None:
    conn = op.get_bind()
    inspector = sa.inspect(conn)

    cna_columns = [c["name"] for c in inspector.get_columns("credit_note_applications")]
    if "updated_at" in cna_columns:
        op.drop_column("credit_note_applications", "updated_at")

    ledger_columns = [c["name"] for c in inspector.get_columns("ledger_entries")]
    if "updated_at" in ledger_columns:
        op.drop_column("ledger_entries", "updated_at")
