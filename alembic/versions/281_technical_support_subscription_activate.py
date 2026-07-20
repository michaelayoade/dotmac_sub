"""Allow technical support to activate suspended subscriptions.

Revision ID: 281_technical_support_subscription_activate
Revises: 280_technical_support_subscription_suspend
"""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import uuid4

import sqlalchemy as sa

from alembic import op

revision = "281_technical_support_subscription_activate"
down_revision = "280_technical_support_subscription_suspend"
branch_labels = None
depends_on = None


PERMISSION_KEY = "subscription:activate"
PERMISSION_DESCRIPTION = "Activate suspended subscriptions"
ROLE_NAME = "Technical support"


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    if not {"roles", "permissions", "role_permissions"}.issubset(
        inspector.get_table_names()
    ):
        return

    now = datetime.now(UTC)
    permission_id = bind.execute(
        sa.text("SELECT id FROM permissions WHERE key = :key"),
        {"key": PERMISSION_KEY},
    ).scalar()
    if permission_id is None:
        permission_id = str(uuid4())
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
                "id": permission_id,
                "key": PERMISSION_KEY,
                "description": PERMISSION_DESCRIPTION,
                "now": now,
            },
        )
    else:
        bind.execute(
            sa.text(
                """
                UPDATE permissions
                SET description = COALESCE(NULLIF(description, ''), :description),
                    is_active = true,
                    is_ui_assignable = true,
                    updated_at = :now
                WHERE id = CAST(:id AS uuid)
                """
            ),
            {
                "id": str(permission_id),
                "description": PERMISSION_DESCRIPTION,
                "now": now,
            },
        )

    bind.execute(
        sa.text(
            """
            INSERT INTO role_permissions (id, role_id, permission_id)
            SELECT (
                   substr(md5(r.id::text || p.id::text), 1, 8) || '-' ||
                   substr(md5(r.id::text || p.id::text), 9, 4) || '-' ||
                   substr(md5(r.id::text || p.id::text), 13, 4) || '-' ||
                   substr(md5(r.id::text || p.id::text), 17, 4) || '-' ||
                   substr(md5(r.id::text || p.id::text), 21, 12)
                   )::uuid,
                   r.id,
                   p.id
            FROM roles r
            JOIN permissions p ON p.key = :permission_key
            WHERE r.name = :role_name
              AND r.is_active = true
              AND p.is_active = true
            ON CONFLICT (role_id, permission_id) DO NOTHING
            """
        ),
        {"role_name": ROLE_NAME, "permission_key": PERMISSION_KEY},
    )


def downgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    if not {"roles", "permissions", "role_permissions"}.issubset(
        inspector.get_table_names()
    ):
        return

    bind.execute(
        sa.text(
            """
            DELETE FROM role_permissions rp
            USING roles r, permissions p
            WHERE rp.role_id = r.id
              AND rp.permission_id = p.id
              AND r.name = :role_name
              AND p.key = :permission_key
            """
        ),
        {"role_name": ROLE_NAME, "permission_key": PERMISSION_KEY},
    )
