"""Drop legacy ont_assignments.active column.

Revision ID: 077_drop_legacy_ont_assignment_active
Revises: 076_add_olt_config_pack_check_constraint
Create Date: 2026-04-28
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "077_drop_legacy_ont_assignment_active"
down_revision = "076_add_olt_config_pack_check_constraint"
branch_labels = None
depends_on = None


def _column_exists(table_name: str, column_name: str) -> bool:
    bind = op.get_bind()
    return bool(
        bind.exec_driver_sql(
            """
            SELECT 1
            FROM information_schema.columns
            WHERE table_schema = current_schema()
              AND table_name = %(table_name)s
              AND column_name = %(column_name)s
            """,
            {"table_name": table_name, "column_name": column_name},
        ).scalar()
    )


def upgrade() -> None:
    if _column_exists("ont_assignments", "is_active") and _column_exists(
        "ont_assignments", "active"
    ):
        op.drop_column("ont_assignments", "active")


def downgrade() -> None:
    if not _column_exists("ont_assignments", "active"):
        op.add_column(
            "ont_assignments",
            sa.Column("active", sa.Boolean(), nullable=True),
        )
        if _column_exists("ont_assignments", "is_active"):
            op.execute("UPDATE ont_assignments SET active = is_active")
        else:
            op.execute("UPDATE ont_assignments SET active = true")
        op.alter_column(
            "ont_assignments",
            "active",
            nullable=False,
            server_default=sa.true(),
        )
