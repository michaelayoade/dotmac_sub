"""Add manager AI inbox insight permission.

Revision ID: 411_inbox_manager_ai_permission
Revises: 410_add_meta_social_notification_channels
"""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import uuid4

import sqlalchemy as sa

from alembic import op

revision = "411_inbox_manager_ai_permission"
down_revision = "410_add_meta_social_notification_channels"
branch_labels = None
depends_on = None

NEW_KEY = "support:inbox_ai:read"
NEW_DESCRIPTION = "Use manager AI insight for inbox conversations"
SOURCE_KEY = "support:ticket:read"


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

    new_id = _permission_id(bind, NEW_KEY)
    if not new_id:
        new_id = str(uuid4())
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
            {"id": new_id, "key": NEW_KEY, "description": NEW_DESCRIPTION, "now": now},
        )

    if "role_permissions" not in tables:
        return
    source_id = _permission_id(bind, SOURCE_KEY)
    if not source_id:
        return
    role_ids = {
        row[0]
        for row in bind.execute(
            sa.text(
                "SELECT DISTINCT role_id FROM role_permissions "
                "WHERE permission_id = :source_id"
            ),
            {"source_id": source_id},
        ).fetchall()
    }
    for role_id in role_ids:
        already = bind.execute(
            sa.text(
                "SELECT 1 FROM role_permissions "
                "WHERE role_id = :r AND permission_id = :p"
            ),
            {"r": role_id, "p": new_id},
        ).scalar()
        if not already:
            bind.execute(
                sa.text(
                    "INSERT INTO role_permissions (id, role_id, permission_id) "
                    "VALUES (:id, :r, :p)"
                ),
                {"id": str(uuid4()), "r": role_id, "p": new_id},
            )


def downgrade() -> None:
    bind = op.get_bind()
    tables = sa.inspect(bind).get_table_names()
    if "permissions" not in tables:
        return
    pid = _permission_id(bind, NEW_KEY)
    if not pid:
        return
    if "role_permissions" in tables:
        bind.execute(
            sa.text("DELETE FROM role_permissions WHERE permission_id = :p"),
            {"p": pid},
        )
    bind.execute(sa.text("DELETE FROM permissions WHERE key = :key"), {"key": NEW_KEY})
