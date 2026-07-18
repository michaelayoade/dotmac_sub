"""Retire the coarse gis:map:edit permission.

Every GIS mutation route now declares a granular
``gis:location:write`` / ``gis:area:write`` / ``gis:layer:write`` /
``gis:location_request:review`` permission, so ``gis:map:edit`` has no remaining
consumer. Migration 347 already granted the granular permissions to every role
that held the coarse one, so deleting it here removes no access.

Revision ID: 348_retire_coarse_gis_edit_permission
Revises: 347_gis_granular_permissions
"""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import uuid4

import sqlalchemy as sa

from alembic import op

revision = "348_retire_coarse_gis_edit_permission"
down_revision = "347_gis_granular_permissions"
branch_labels = None
depends_on = None

COARSE_KEY = "gis:map:edit"
COARSE_DESCRIPTION = "Edit map features (markers, polygons)"


def upgrade() -> None:
    bind = op.get_bind()
    tables = sa.inspect(bind).get_table_names()
    if "permissions" not in tables:
        return
    pid = bind.execute(
        sa.text("SELECT id FROM permissions WHERE key = :key"), {"key": COARSE_KEY}
    ).scalar()
    if not pid:
        return
    if "role_permissions" in tables:
        bind.execute(
            sa.text("DELETE FROM role_permissions WHERE permission_id = :p"),
            {"p": pid},
        )
    bind.execute(sa.text("DELETE FROM permissions WHERE id = :p"), {"p": pid})


def downgrade() -> None:
    bind = op.get_bind()
    tables = sa.inspect(bind).get_table_names()
    if "permissions" not in tables:
        return
    existing = bind.execute(
        sa.text("SELECT id FROM permissions WHERE key = :key"), {"key": COARSE_KEY}
    ).scalar()
    if existing:
        return
    now = datetime.now(UTC)
    bind.execute(
        sa.text(
            """
            INSERT INTO permissions (
                id, key, description, is_active, is_ui_assignable,
                created_at, updated_at
            )
            VALUES (:id, :key, :description, true, true, :now, :now)
            """
        ),
        {
            "id": str(uuid4()),
            "key": COARSE_KEY,
            "description": COARSE_DESCRIPTION,
            "now": now,
        },
    )
