"""Add granular GIS permissions and grant them to coarse-permission holders.

The admin GIS surface is split from the coarse ``gis:map:edit`` into
``gis:location:write`` / ``gis:area:write`` / ``gis:layer:write`` and a
separate ``gis:location_request:review`` — because approving a customer's
location-change request is a distinct business decision from editing a map
layer. This seeds the granular permissions on existing databases and grants all
four to every role that already holds ``gis:map:edit``, so no principal loses
access. The coarse permission is retired by the following migration.

Revision ID: 360_gis_granular_permissions
Revises: 359_payment_prepaid_applications
"""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import uuid4

import sqlalchemy as sa

from alembic import op

revision = "360_gis_granular_permissions"
down_revision = "359_payment_prepaid_applications"
branch_labels = None
depends_on = None

COARSE_KEY = "gis:map:edit"
GRANULAR = [
    ("gis:location:write", "Create, edit, and delete map location markers"),
    ("gis:area:write", "Create, edit, and delete map coverage areas"),
    ("gis:layer:write", "Create, edit, and delete map layers"),
    (
        "gis:location_request:review",
        "Approve or reject customer location-change requests",
    ),
]


def _permission_id(bind, key: str):
    return bind.execute(
        sa.text("SELECT id FROM permissions WHERE key = :key"), {"key": key}
    ).scalar()


def upgrade() -> None:
    bind = op.get_bind()
    tables = sa.inspect(bind).get_table_names()
    if "permissions" not in tables:
        return
    now = datetime.now(UTC)

    granular_ids: dict[str, str] = {}
    for key, description in GRANULAR:
        pid = _permission_id(bind, key)
        if not pid:
            pid = str(uuid4())
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
                {"id": pid, "key": key, "description": description, "now": now},
            )
        granular_ids[key] = pid

    if "role_permissions" not in tables:
        return
    coarse_id = _permission_id(bind, COARSE_KEY)
    if not coarse_id:
        return
    role_ids = [
        row[0]
        for row in bind.execute(
            sa.text("SELECT role_id FROM role_permissions WHERE permission_id = :p"),
            {"p": coarse_id},
        ).fetchall()
    ]
    for role_id in role_ids:
        for pid in granular_ids.values():
            already = bind.execute(
                sa.text(
                    "SELECT 1 FROM role_permissions "
                    "WHERE role_id = :r AND permission_id = :p"
                ),
                {"r": role_id, "p": pid},
            ).scalar()
            if not already:
                bind.execute(
                    sa.text(
                        "INSERT INTO role_permissions (id, role_id, permission_id) "
                        "VALUES (:id, :r, :p)"
                    ),
                    {"id": str(uuid4()), "r": role_id, "p": pid},
                )


def downgrade() -> None:
    bind = op.get_bind()
    tables = sa.inspect(bind).get_table_names()
    if "permissions" not in tables:
        return
    keys = [key for key, _ in GRANULAR]
    if "role_permissions" in tables:
        for key in keys:
            pid = _permission_id(bind, key)
            if pid:
                bind.execute(
                    sa.text("DELETE FROM role_permissions WHERE permission_id = :p"),
                    {"p": pid},
                )
    for key in keys:
        bind.execute(sa.text("DELETE FROM permissions WHERE key = :key"), {"key": key})
