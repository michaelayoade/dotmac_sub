"""Add paystack to PaymentProviderType enum.

Revision ID: u1v2w3x4y5z6
Revises: t8u9v0w1x2y3
Create Date: 2026-02-22 00:00:00.000000

"""

from collections.abc import Sequence

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "u1v2w3x4y5z6"
down_revision: str = "t8u9v0w1x2y3"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # ALTER TYPE ... ADD VALUE is idempotent-safe: Postgres raises
    # DuplicateObject if the value already exists, which we catch.
    op.execute("""
        DO $$
        BEGIN
            ALTER TYPE paymentprovidertype ADD VALUE IF NOT EXISTS 'paystack';
        EXCEPTION WHEN duplicate_object THEN
            NULL;
        END
        $$;
    """)


def downgrade() -> None:
    # PostgreSQL does not support removing values from an enum type.
    # The value is harmless if left in place.
    pass
