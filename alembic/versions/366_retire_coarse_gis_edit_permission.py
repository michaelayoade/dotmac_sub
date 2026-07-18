"""Retire the coarse gis:map:edit permission.

Every GIS mutation route now declares a granular
``gis:location:write`` / ``gis:area:write`` / ``gis:layer:write`` /
``gis:location_request:review`` permission, so ``gis:map:edit`` has no remaining
consumer. Migration 365 already granted the granular permissions to every role
that held the coarse one, so deleting it here removes no access.

Revision ID: 366_retire_coarse_gis_edit_permission
Revises: 365_gis_granular_permissions
"""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import uuid4

import sqlalchemy as sa

from alembic import op

revision = "366_retire_coarse_gis_edit_permission"
down_revision = "365_gis_granular_permissions"
branch_labels = None
depends_on = None

COARSE_KEY = "gis:map:edit"
COARSE_DESCRIPTION = "Edit map features (markers, polygons)"
GRANULAR_KEYS = (
    "gis:location:write",
    "gis:area:write",
    "gis:layer:write",
    "gis:location_request:review",
)


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
    coarse_id = bind.execute(
        sa.text("SELECT id FROM permissions WHERE key = :key"), {"key": COARSE_KEY}
    ).scalar()
    if not coarse_id:
        coarse_id = str(uuid4())
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
                "id": coarse_id,
                "key": COARSE_KEY,
                "description": COARSE_DESCRIPTION,
                "now": now,
            },
        )

    if "role_permissions" not in tables:
        return
    # Revision 360 granted every former coarse holder all four granular
    # permissions. Restore the coarse grant for roles that still hold that
    # complete capability set so rolling back both revisions does not silently
    # remove GIS edit access.
    complete_holder_ids = [
        row[0]
        for row in bind.execute(
            sa.text(
                """
                SELECT rp.role_id
                FROM role_permissions rp
                JOIN permissions p ON p.id = rp.permission_id
                WHERE p.key IN :keys
                GROUP BY rp.role_id
                HAVING COUNT(DISTINCT p.key) = :key_count
                """
            ).bindparams(sa.bindparam("keys", expanding=True)),
            {"keys": list(GRANULAR_KEYS), "key_count": len(GRANULAR_KEYS)},
        ).fetchall()
    ]
    for role_id in complete_holder_ids:
        already = bind.execute(
            sa.text(
                "SELECT 1 FROM role_permissions "
                "WHERE role_id = :role_id AND permission_id = :permission_id"
            ),
            {"role_id": role_id, "permission_id": coarse_id},
        ).scalar()
        if not already:
            bind.execute(
                sa.text(
                    "INSERT INTO role_permissions (id, role_id, permission_id) "
                    "VALUES (:id, :role_id, :permission_id)"
                ),
                {
                    "id": str(uuid4()),
                    "role_id": role_id,
                    "permission_id": coarse_id,
                },
            )
