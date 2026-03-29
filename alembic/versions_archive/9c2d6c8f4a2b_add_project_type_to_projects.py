"""add project type to projects

Revision ID: 9c2d6c8f4a2b
Revises: 6ebaf94af561
Create Date: 2026-01-12 00:00:00.000000

"""
from typing import Sequence, Union

from alembic import op


# revision identifiers, used by Alembic.
revision: str = "9c2d6c8f4a2b"
down_revision: Union[str, None] = "6ebaf94af561"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute(
        """
        DO $$ BEGIN
            CREATE TYPE projecttype AS ENUM (
                'cable_rerun',
                'fiber_optics_relocation',
                'radio_fiber_relocation',
                'fiber_optics_installation',
                'radio_installation'
            );
        EXCEPTION
            WHEN duplicate_object THEN null;
        END $$;
        """
    )
    op.execute(
        """
        ALTER TABLE projects
        ADD COLUMN IF NOT EXISTS project_type projecttype
        """
    )


def downgrade() -> None:
    op.execute(
        """
        ALTER TABLE projects
        DROP COLUMN IF EXISTS project_type
        """
    )
    op.execute("DROP TYPE IF EXISTS projecttype")
