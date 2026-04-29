"""Clean ONT status persisted model.

Revision ID: 078_clean_ont_status_model
Revises: 077_drop_legacy_ont_assignment_active
Create Date: 2026-04-29
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "078_clean_ont_status_model"
down_revision = "077_drop_legacy_ont_assignment_active"
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
    if not _column_exists("ont_units", "olt_status_seen_at"):
        op.add_column(
            "ont_units",
            sa.Column("olt_status_seen_at", sa.DateTime(timezone=True), nullable=True),
        )

    if _column_exists("ont_units", "online_status") and not _column_exists(
        "ont_units", "olt_status"
    ):
        op.alter_column("ont_units", "online_status", new_column_name="olt_status")

    if _column_exists("ont_units", "status_resolved_at") and _column_exists(
        "ont_units", "olt_status_seen_at"
    ):
        op.execute(
            """
            UPDATE ont_units
            SET olt_status_seen_at = status_resolved_at
            WHERE olt_status_seen_at IS NULL
              AND effective_status_source = 'olt'
            """
        )

    if _column_exists("ont_units", "acs_status"):
        op.drop_column("ont_units", "acs_status")
    if _column_exists("ont_units", "status_resolved_at"):
        op.drop_column("ont_units", "status_resolved_at")


def downgrade() -> None:
    if not _column_exists("ont_units", "status_resolved_at"):
        op.add_column(
            "ont_units",
            sa.Column("status_resolved_at", sa.DateTime(timezone=True), nullable=True),
        )
    if not _column_exists("ont_units", "acs_status"):
        ont_acs_status = sa.Enum(
            "online",
            "stale",
            "unmanaged",
            "unknown",
            name="ontacsstatus",
            create_type=False,
        )
        op.add_column(
            "ont_units",
            sa.Column(
                "acs_status",
                ont_acs_status,
                nullable=False,
                server_default="unknown",
            ),
        )

    if _column_exists("ont_units", "olt_status") and not _column_exists(
        "ont_units", "online_status"
    ):
        op.alter_column("ont_units", "olt_status", new_column_name="online_status")

    if _column_exists("ont_units", "olt_status_seen_at"):
        op.drop_column("ont_units", "olt_status_seen_at")
