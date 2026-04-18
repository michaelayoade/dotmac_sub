"""Add is_active column to payment_allocations for soft-delete support.

Revision ID: j4k5l6m7n8o9
Revises: i3j4k5l6m7n8
Create Date: 2026-03-16
"""

import sqlalchemy as sa

from alembic import op

revision = "j4k5l6m7n8o9"
down_revision = "i3j4k5l6m7n8"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Idempotent: check before adding
    conn = op.get_bind()
    inspector = sa.inspect(conn)
    columns = [c["name"] for c in inspector.get_columns("payment_allocations")]
    if "is_active" not in columns:
        op.add_column(
            "payment_allocations",
            sa.Column("is_active", sa.Boolean(), server_default="true", nullable=False),
        )


def downgrade() -> None:
    conn = op.get_bind()
    inspector = sa.inspect(conn)
    columns = [c["name"] for c in inspector.get_columns("payment_allocations")]
    if "is_active" in columns:
        op.drop_column("payment_allocations", "is_active")
