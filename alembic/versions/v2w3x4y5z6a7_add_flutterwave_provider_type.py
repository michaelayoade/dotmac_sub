"""Add flutterwave to PaymentProviderType enum.

Revision ID: v2w3x4y5z6a7
Revises: u1v2w3x4y5z6
Create Date: 2026-02-22 00:00:00.000000

"""

from collections.abc import Sequence

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "v2w3x4y5z6a7"
down_revision: str = "u1v2w3x4y5z6"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute("""
        DO $$
        BEGIN
            ALTER TYPE paymentprovidertype ADD VALUE IF NOT EXISTS 'flutterwave';
        EXCEPTION WHEN duplicate_object THEN
            NULL;
        END
        $$;
    """)


def downgrade() -> None:
    # PostgreSQL does not support removing values from an enum type.
    # The value is harmless if left in place.
    pass
