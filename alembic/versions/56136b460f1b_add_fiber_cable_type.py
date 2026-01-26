"""Add fiber cable type and fiber count to fiber_segments

Revision ID: 56136b460f1b
Revises: 799a0ecebdd4
Create Date: 2026-01-09 10:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '56136b460f1b'
down_revision: Union[str, None] = '799a0ecebdd4'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # Create the fibercabletype enum
    op.execute("""
        DO $$ BEGIN
            CREATE TYPE fibercabletype AS ENUM (
                'single_mode', 'multi_mode', 'armored', 'aerial', 'underground', 'direct_buried'
            );
        EXCEPTION
            WHEN duplicate_object THEN null;
        END $$;
    """)

    # Add columns if they don't already exist (safe for partially applied DBs).
    op.execute(
        """
        ALTER TABLE fiber_segments
        ADD COLUMN IF NOT EXISTS cable_type fibercabletype
        """
    )
    op.execute(
        """
        ALTER TABLE fiber_segments
        ADD COLUMN IF NOT EXISTS fiber_count INTEGER
        """
    )


def downgrade() -> None:
    # Remove columns
    op.execute(
        """
        ALTER TABLE fiber_segments
        DROP COLUMN IF EXISTS fiber_count
        """
    )
    op.execute(
        """
        ALTER TABLE fiber_segments
        DROP COLUMN IF EXISTS cable_type
        """
    )

    # Drop enum type
    op.execute("DROP TYPE IF EXISTS fibercabletype")
