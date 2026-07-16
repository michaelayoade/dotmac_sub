"""Retire the coarse operations:dispatch permission.

The app.api.dispatch router is now mounted with an operations:dispatch:read floor
and each mutating endpoint declares operations:dispatch:write/:assign, so the coarse
``operations:dispatch`` permission has no remaining consumer. Migration 311 already
granted the granular permissions to every role that held the coarse one, so deleting
it here removes no access.

Revision ID: 312_retire_coarse_dispatch_permission
Revises: 311_dispatch_granular_permissions
"""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import uuid4

import sqlalchemy as sa

from alembic import op

revision = "312_retire_coarse_dispatch_permission"
down_revision = "311_dispatch_granular_permissions"
branch_labels = None
depends_on = None

COARSE_KEY = "operations:dispatch"
COARSE_DESCRIPTION = "Dispatch operational work"


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
