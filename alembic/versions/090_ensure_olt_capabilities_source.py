"""Ensure OLT capabilities source column exists.

Revision ID: 090_ensure_olt_capabilities_source
Revises: 089_add_imported_olt_profile_state
Create Date: 2026-05-06
"""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision = "090_ensure_olt_capabilities_source"
down_revision = "089_add_imported_olt_profile_state"
branch_labels = None
depends_on = None


def _column_exists(table_name: str, column_name: str) -> bool:
    inspector = sa.inspect(op.get_bind())
    return column_name in {col["name"] for col in inspector.get_columns(table_name)}


def _constraint_exists(table_name: str, constraint_name: str) -> bool:
    inspector = sa.inspect(op.get_bind())
    return constraint_name in {
        constraint["name"] for constraint in inspector.get_check_constraints(table_name)
    }


def upgrade() -> None:
    if not _column_exists("olt_devices", "capabilities_source"):
        op.add_column(
            "olt_devices",
            sa.Column(
                "capabilities_source",
                sa.String(length=20),
                nullable=False,
                server_default="auto",
            ),
        )
    if not _constraint_exists("olt_devices", "ck_olt_devices_capabilities_source"):
        op.create_check_constraint(
            "ck_olt_devices_capabilities_source",
            "olt_devices",
            "capabilities_source IN ('auto', 'manual')",
        )


def downgrade() -> None:
    if _constraint_exists("olt_devices", "ck_olt_devices_capabilities_source"):
        op.drop_constraint(
            "ck_olt_devices_capabilities_source",
            "olt_devices",
            type_="check",
        )
    if _column_exists("olt_devices", "capabilities_source"):
        op.drop_column("olt_devices", "capabilities_source")
