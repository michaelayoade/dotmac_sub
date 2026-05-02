"""Drop combined ONT effective status columns.

Revision ID: 083_drop_ont_effective_status
Revises: 082_zbx_signal_health
Create Date: 2026-05-02
"""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision = "083_drop_ont_effective_status"
down_revision = "082_zbx_signal_health"
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
    for column_name in (
        "consecutive_offline_polls",
        "effective_status_source",
        "effective_status",
    ):
        if _column_exists("ont_units", column_name):
            op.drop_column("ont_units", column_name)


def downgrade() -> None:
    online_status = sa.Enum(
        "online",
        "offline",
        name="onteffectivestatus",
        create_type=False,
    )
    status_source = sa.Enum(
        "olt",
        "acs",
        "derived",
        name="ontstatussource",
        create_type=False,
    )
    if not _column_exists("ont_units", "effective_status"):
        op.add_column(
            "ont_units",
            sa.Column(
                "effective_status",
                online_status,
                nullable=False,
                server_default="offline",
            ),
        )
    if not _column_exists("ont_units", "effective_status_source"):
        op.add_column(
            "ont_units",
            sa.Column(
                "effective_status_source",
                status_source,
                nullable=False,
                server_default="derived",
            ),
        )
    if not _column_exists("ont_units", "consecutive_offline_polls"):
        op.add_column(
            "ont_units",
            sa.Column(
                "consecutive_offline_polls",
                sa.Integer(),
                nullable=False,
                server_default="0",
            ),
        )
