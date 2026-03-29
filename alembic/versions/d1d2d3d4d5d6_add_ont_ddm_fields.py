"""add ont ddm health telemetry fields

Revision ID: d1d2d3d4d5d6
Revises: aa1b2c3d4e5f
Create Date: 2026-03-29 00:00:00.000000
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "d1d2d3d4d5d6"
down_revision: str = "aa1b2c3d4e5f"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    conn = op.get_bind()
    inspector = sa.inspect(conn)
    columns = [c["name"] for c in inspector.get_columns("ont_units")]

    if "onu_tx_signal_dbm" not in columns:
        op.add_column(
            "ont_units", sa.Column("onu_tx_signal_dbm", sa.Float(), nullable=True)
        )
    if "ont_temperature_c" not in columns:
        op.add_column(
            "ont_units", sa.Column("ont_temperature_c", sa.Float(), nullable=True)
        )
    if "ont_voltage_v" not in columns:
        op.add_column(
            "ont_units", sa.Column("ont_voltage_v", sa.Float(), nullable=True)
        )
    if "ont_bias_current_ma" not in columns:
        op.add_column(
            "ont_units", sa.Column("ont_bias_current_ma", sa.Float(), nullable=True)
        )


def downgrade() -> None:
    conn = op.get_bind()
    inspector = sa.inspect(conn)
    columns = [c["name"] for c in inspector.get_columns("ont_units")]

    for col in [
        "ont_bias_current_ma",
        "ont_voltage_v",
        "ont_temperature_c",
        "onu_tx_signal_dbm",
    ]:
        if col in columns:
            op.drop_column("ont_units", col)
