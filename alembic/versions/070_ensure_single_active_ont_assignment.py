"""Ensure only one active assignment per ONT.

Revision ID: 070_single_active_ont_assignment
Revises: 069_wcd_indices
Create Date: 2026-04-26
"""

from __future__ import annotations

from alembic import op

revision = "070_single_active_ont_assignment"
down_revision = "069_wcd_indices"
branch_labels = None
depends_on = None


def _active_column_name() -> str:
    bind = op.get_bind()
    rows = bind.exec_driver_sql(
        """
        SELECT column_name
        FROM information_schema.columns
        WHERE table_name = 'ont_assignments'
          AND column_name IN ('is_active', 'active')
        ORDER BY CASE column_name WHEN 'is_active' THEN 0 ELSE 1 END
        """
    ).fetchall()
    if not rows:
        raise RuntimeError("ont_assignments active column not found")
    return str(rows[0][0])


def upgrade() -> None:
    bind = op.get_bind()
    active_column = _active_column_name()

    if active_column == "is_active":
        duplicates = bind.exec_driver_sql(
            """
            SELECT ont_unit_id, count(*) AS active_count
            FROM ont_assignments
            WHERE is_active IS TRUE
            GROUP BY ont_unit_id
            HAVING count(*) > 1
            LIMIT 10
            """
        ).fetchall()
    else:
        duplicates = bind.exec_driver_sql(
            """
            SELECT ont_unit_id, count(*) AS active_count
            FROM ont_assignments
            WHERE active IS TRUE
            GROUP BY ont_unit_id
            HAVING count(*) > 1
            LIMIT 10
            """
        ).fetchall()
    if duplicates:
        sample = ", ".join(f"{row[0]} ({row[1]})" for row in duplicates)
        raise RuntimeError(
            "Cannot create ix_ont_assignments_active_unit; duplicate active "
            f"ONT assignments exist: {sample}"
        )

    existing = bind.exec_driver_sql(
        """
        SELECT indexdef
        FROM pg_indexes
        WHERE schemaname = current_schema()
          AND tablename = 'ont_assignments'
          AND indexname = 'ix_ont_assignments_active_unit'
        """
    ).scalar()
    if existing and f"WHERE ({active_column} IS TRUE)" in str(existing):
        return
    if existing:
        bind.exec_driver_sql("DROP INDEX ix_ont_assignments_active_unit")

    if active_column == "is_active":
        bind.exec_driver_sql(
            """
            CREATE UNIQUE INDEX ix_ont_assignments_active_unit
            ON ont_assignments (ont_unit_id)
            WHERE is_active IS TRUE
            """
        )
    else:
        bind.exec_driver_sql(
            """
            CREATE UNIQUE INDEX ix_ont_assignments_active_unit
            ON ont_assignments (ont_unit_id)
            WHERE active IS TRUE
            """
        )


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS ix_ont_assignments_active_unit")
