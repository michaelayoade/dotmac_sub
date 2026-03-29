"""Add monitoring RBAC permissions.

Revision ID: m9n8o7p6q5r4
Revises: x5y6z7a8b9c0
Create Date: 2026-03-05
"""

from collections.abc import Sequence
from datetime import UTC, datetime
from uuid import uuid4

import sqlalchemy as sa

from alembic import op

revision: str = "m9n8o7p6q5r4"
down_revision: str | Sequence[str] | None = "x5y6z7a8b9c0"
branch_labels: Sequence[str] | None = None
depends_on: Sequence[str] | None = None


def upgrade() -> None:
    bind = op.get_bind()
    now = datetime.now(UTC)

    permission_values = [
        ("monitoring:read", "View monitoring dashboards and alerts"),
        ("monitoring:write", "Manage monitoring rules and alert states"),
    ]

    permission_ids: dict[str, str] = {}
    for key, description in permission_values:
        existing_permission_id = bind.execute(
            sa.text("SELECT id FROM permissions WHERE key = :key"),
            {"key": key},
        ).scalar()
        if existing_permission_id:
            permission_ids[key] = str(existing_permission_id)
            bind.execute(
                sa.text(
                    """
                    UPDATE permissions
                    SET is_active = true, updated_at = :updated_at
                    WHERE id = :permission_id
                    """
                ),
                {"updated_at": now, "permission_id": existing_permission_id},
            )
            continue

        new_permission_id = str(uuid4())
        bind.execute(
            sa.text(
                """
                INSERT INTO permissions (id, key, description, is_active, created_at, updated_at)
                VALUES (:id, :key, :description, true, :created_at, :updated_at)
                """
            ),
            {
                "id": new_permission_id,
                "key": key,
                "description": description,
                "created_at": now,
                "updated_at": now,
            },
        )
        permission_ids[key] = new_permission_id

    # Grant monitoring permissions to any role already granted the analogous network permission.
    read_role_ids = bind.execute(
        sa.text(
            """
            SELECT rp.role_id
            FROM role_permissions rp
            JOIN permissions p ON p.id = rp.permission_id
            WHERE p.key = 'network:read'
            """
        )
    ).scalars()

    for role_id in read_role_ids:
        bind.execute(
            sa.text(
                """
                INSERT INTO role_permissions (id, role_id, permission_id)
                VALUES (:id, :role_id, :permission_id)
                ON CONFLICT (role_id, permission_id) DO NOTHING
                """
            ),
            {
                "id": str(uuid4()),
                "role_id": role_id,
                "permission_id": permission_ids["monitoring:read"],
            },
        )

    write_role_ids = bind.execute(
        sa.text(
            """
            SELECT rp.role_id
            FROM role_permissions rp
            JOIN permissions p ON p.id = rp.permission_id
            WHERE p.key = 'network:write'
            """
        )
    ).scalars()

    for role_id in write_role_ids:
        bind.execute(
            sa.text(
                """
                INSERT INTO role_permissions (id, role_id, permission_id)
                VALUES (:id, :role_id, :permission_id)
                ON CONFLICT (role_id, permission_id) DO NOTHING
                """
            ),
            {
                "id": str(uuid4()),
                "role_id": role_id,
                "permission_id": permission_ids["monitoring:write"],
            },
        )


def downgrade() -> None:
    bind = op.get_bind()
    bind.execute(
        sa.text(
            """
            DELETE FROM role_permissions
            WHERE permission_id IN (
                SELECT id FROM permissions WHERE key IN ('monitoring:read', 'monitoring:write')
            );
            """
        )
    )
    bind.execute(
        sa.text(
            """
            DELETE FROM permissions
            WHERE key IN ('monitoring:read', 'monitoring:write');
            """
        )
    )
