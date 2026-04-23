"""Add default_provisioning_profile_id to OLT devices.

Revision ID: 054_add_olt_default_prov_profile
Revises: 053_add_wan_instance_components
Create Date: 2026-04-23
"""

from __future__ import annotations

import sqlalchemy as sa
from sqlalchemy import inspect
from sqlalchemy.dialects.postgresql import UUID

from alembic import op

revision = "054_add_olt_default_prov_profile"
down_revision = "053_add_wan_instance_components"
branch_labels = None
depends_on = None

_TABLE = "olt_devices"
_COLUMN = "default_provisioning_profile_id"


def _has_column(conn, table: str, column: str) -> bool:
    columns = {c["name"] for c in inspect(conn).get_columns(table)}
    return column in columns


def upgrade() -> None:
    conn = op.get_bind()
    if _has_column(conn, _TABLE, _COLUMN):
        return

    op.add_column(
        _TABLE,
        sa.Column(
            _COLUMN,
            UUID(as_uuid=True),
            sa.ForeignKey(
                "ont_provisioning_profiles.id",
                ondelete="SET NULL",
                name="fk_olt_devices_default_provisioning_profile",
            ),
            nullable=True,
        ),
    )


def downgrade() -> None:
    conn = op.get_bind()
    if not _has_column(conn, _TABLE, _COLUMN):
        return

    op.drop_constraint(
        "fk_olt_devices_default_provisioning_profile", _TABLE, type_="foreignkey"
    )
    op.drop_column(_TABLE, _COLUMN)
