"""add OLT observed snapshot columns

Revision ID: 044_add_olt_observed_snapshots
Revises: 043_add_pending_tr069_job_status
Create Date: 2026-04-20
"""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision = "044_add_olt_observed_snapshots"
down_revision = "043_add_pending_tr069_job_status"
branch_labels = None
depends_on = None


def _has_column(table_name: str, column_name: str) -> bool:
    inspector = sa.inspect(op.get_bind())
    return column_name in {
        column["name"] for column in inspector.get_columns(table_name)
    }


def upgrade() -> None:
    if not _has_column("olt_devices", "tr069_profiles_snapshot"):
        op.add_column("olt_devices", sa.Column("tr069_profiles_snapshot", sa.JSON()))
    if not _has_column("olt_devices", "tr069_profiles_snapshot_at"):
        op.add_column(
            "olt_devices",
            sa.Column("tr069_profiles_snapshot_at", sa.DateTime(timezone=True)),
        )
    if not _has_column("ont_units", "olt_observed_snapshot"):
        op.add_column("ont_units", sa.Column("olt_observed_snapshot", sa.JSON()))
    if not _has_column("ont_units", "olt_observed_snapshot_at"):
        op.add_column(
            "ont_units",
            sa.Column("olt_observed_snapshot_at", sa.DateTime(timezone=True)),
        )


def downgrade() -> None:
    if _has_column("ont_units", "olt_observed_snapshot_at"):
        op.drop_column("ont_units", "olt_observed_snapshot_at")
    if _has_column("ont_units", "olt_observed_snapshot"):
        op.drop_column("ont_units", "olt_observed_snapshot")
    if _has_column("olt_devices", "tr069_profiles_snapshot_at"):
        op.drop_column("olt_devices", "tr069_profiles_snapshot_at")
    if _has_column("olt_devices", "tr069_profiles_snapshot"):
        op.drop_column("olt_devices", "tr069_profiles_snapshot")
