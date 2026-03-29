"""add ONT optical signal monitoring fields

Revision ID: c4d5e6f7a8b9
Revises: b3c4d5e6f7a8
Create Date: 2026-02-24 18:00:00.000000
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "c4d5e6f7a8b9"
down_revision: str = "b3c4d5e6f7a8"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # Create enums if they don't exist
    conn = op.get_bind()
    inspector = sa.inspect(conn)

    # OnuOnlineStatus enum
    result = conn.execute(
        sa.text("SELECT 1 FROM pg_type WHERE typname = 'onuonlinestatus'")
    )
    if not result.fetchone():
        op.execute(
            "CREATE TYPE onuonlinestatus AS ENUM ('online', 'offline', 'unknown')"
        )

    # OnuOfflineReason enum
    result = conn.execute(
        sa.text("SELECT 1 FROM pg_type WHERE typname = 'onuofflinereason'")
    )
    if not result.fetchone():
        op.execute(
            "CREATE TYPE onuofflinereason AS ENUM ('power_fail', 'los', 'dying_gasp', 'unknown')"
        )

    # Add columns to ont_units if they don't exist
    columns = [c["name"] for c in inspector.get_columns("ont_units")]

    if "onu_rx_signal_dbm" not in columns:
        op.add_column(
            "ont_units",
            sa.Column("onu_rx_signal_dbm", sa.Float(), nullable=True),
        )
    if "olt_rx_signal_dbm" not in columns:
        op.add_column(
            "ont_units",
            sa.Column("olt_rx_signal_dbm", sa.Float(), nullable=True),
        )
    if "distance_meters" not in columns:
        op.add_column(
            "ont_units",
            sa.Column("distance_meters", sa.Integer(), nullable=True),
        )
    if "signal_updated_at" not in columns:
        op.add_column(
            "ont_units",
            sa.Column("signal_updated_at", sa.DateTime(timezone=True), nullable=True),
        )
    if "online_status" not in columns:
        op.add_column(
            "ont_units",
            sa.Column(
                "online_status",
                sa.Enum("online", "offline", "unknown", name="onuonlinestatus", create_type=False),
                server_default="unknown",
                nullable=False,
            ),
        )
    if "last_seen_at" not in columns:
        op.add_column(
            "ont_units",
            sa.Column("last_seen_at", sa.DateTime(timezone=True), nullable=True),
        )
    if "offline_reason" not in columns:
        op.add_column(
            "ont_units",
            sa.Column(
                "offline_reason",
                sa.Enum("power_fail", "los", "dying_gasp", "unknown", name="onuofflinereason", create_type=False),
                nullable=True,
            ),
        )

    # Index on online_status for dashboard aggregation queries
    op.create_index(
        "ix_ont_units_online_status",
        "ont_units",
        ["online_status"],
        if_not_exists=True,
    )


def downgrade() -> None:
    op.drop_index("ix_ont_units_online_status", table_name="ont_units", if_exists=True)
    op.drop_column("ont_units", "offline_reason")
    op.drop_column("ont_units", "last_seen_at")
    op.drop_column("ont_units", "online_status")
    op.drop_column("ont_units", "signal_updated_at")
    op.drop_column("ont_units", "distance_meters")
    op.drop_column("ont_units", "olt_rx_signal_dbm")
    op.drop_column("ont_units", "onu_rx_signal_dbm")
    op.execute("DROP TYPE IF EXISTS onuofflinereason")
    op.execute("DROP TYPE IF EXISTS onuonlinestatus")
