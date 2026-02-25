"""hide activation_state from customer default table columns

Revision ID: d5e6f7a8b9c0
Revises: c4d5e6f7a8b9
Create Date: 2026-02-24 19:10:00.000000
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "d5e6f7a8b9c0"
down_revision: str | None = "c4d5e6f7a8b9"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute(
        sa.text(
            """
            UPDATE table_column_default_config
            SET is_visible = FALSE
            WHERE table_key = :table_key
              AND column_key = :column_key
            """
        ).bindparams(table_key="customers", column_key="activation_state")
    )


def downgrade() -> None:
    op.execute(
        sa.text(
            """
            UPDATE table_column_default_config
            SET is_visible = TRUE
            WHERE table_key = :table_key
              AND column_key = :column_key
            """
        ).bindparams(table_key="customers", column_key="activation_state")
    )
