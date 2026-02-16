"""Drop Payment.invoice_id column - use PaymentAllocation instead.

Revision ID: r5s6t7u8v9w0
Revises: q4r5s6t7u8v9
Create Date: 2026-02-16 00:00:00.000000

"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "r5s6t7u8v9w0"
down_revision: str = "q4r5s6t7u8v9"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # Drop the foreign key constraint and column
    # Check if the constraint exists first (idempotent)
    conn = op.get_bind()
    inspector = sa.inspect(conn)
    columns = [col["name"] for col in inspector.get_columns("payments")]
    if "invoice_id" in columns:
        # Find and drop the foreign key constraint
        fks = inspector.get_foreign_keys("payments")
        for fk in fks:
            if "invoice_id" in fk["constrained_columns"]:
                op.drop_constraint(fk["name"], "payments", type_="foreignkey")
        op.drop_column("payments", "invoice_id")


def downgrade() -> None:
    # Re-add the column
    op.add_column(
        "payments",
        sa.Column("invoice_id", postgresql.UUID(as_uuid=True), nullable=True),
    )
    op.create_foreign_key(
        "payments_invoice_id_fkey",
        "payments",
        "invoices",
        ["invoice_id"],
        ["id"],
    )
