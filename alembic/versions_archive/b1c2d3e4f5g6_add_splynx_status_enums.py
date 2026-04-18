"""Add Splynx-aligned status enum values.

Adds missing status values to align with Splynx lifecycle:
- subscriberstatus: +new, +disabled
- subscriptionstatus: +stopped, +disabled, +archived

Revision ID: b1c2d3e4f5g6
Revises: a1b2c3d4e5f6
Create Date: 2026-03-19
"""

from collections.abc import Sequence

from alembic import op

revision: str = "b1c2d3e4f5g6"
down_revision: str | Sequence[str] | None = "a079511c71a3"
branch_labels: Sequence[str] | None = None
depends_on: Sequence[str] | None = None


def upgrade() -> None:
    import sqlalchemy as sa

    bind = op.get_bind()
    # Only add values if the enums already exist (may not on fresh DBs
    # where the enum is created with all values in a later migration).
    existing_enums = {
        row[0]
        for row in bind.execute(
            sa.text("SELECT typname FROM pg_type WHERE typtype = 'e'")
        )
    }

    if "subscriberstatus" in existing_enums:
        op.execute("ALTER TYPE subscriberstatus ADD VALUE IF NOT EXISTS 'new'")
        op.execute("ALTER TYPE subscriberstatus ADD VALUE IF NOT EXISTS 'disabled'")

    if "subscriptionstatus" in existing_enums:
        op.execute("ALTER TYPE subscriptionstatus ADD VALUE IF NOT EXISTS 'stopped'")
        op.execute("ALTER TYPE subscriptionstatus ADD VALUE IF NOT EXISTS 'disabled'")
        op.execute("ALTER TYPE subscriptionstatus ADD VALUE IF NOT EXISTS 'archived'")


def downgrade() -> None:
    # PostgreSQL does not support removing enum values.
    # The values will remain but won't be used if code reverts.
    pass
