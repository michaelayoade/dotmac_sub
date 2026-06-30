"""Expand technical support network read access.

Revision ID: 187_technical_support_network_reads
Revises: 186_seed_technical_support_role
"""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import uuid4

import sqlalchemy as sa

from alembic import op

revision = "187_technical_support_network_reads"
down_revision = "186_seed_technical_support_role"
branch_labels = None
depends_on = None


ROLE_NAME = "Technical support"
ROLE_DESCRIPTION = (
    "Customer support access with network read and ONT management permissions"
)


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    if not {"roles", "permissions", "role_permissions"}.issubset(
        inspector.get_table_names()
    ):
        return

    now = datetime.now(UTC)
    role_id = bind.execute(
        sa.text("SELECT id FROM roles WHERE name = :name"),
        {"name": ROLE_NAME},
    ).scalar()
    if role_id is None:
        role_id = str(uuid4())
        bind.execute(
            sa.text(
                """
                INSERT INTO roles (id, name, description, is_active, created_at, updated_at)
                VALUES (:id, :name, :description, true, :now, :now)
                """
            ),
            {
                "id": role_id,
                "name": ROLE_NAME,
                "description": ROLE_DESCRIPTION,
                "now": now,
            },
        )
    else:
        bind.execute(
            sa.text(
                """
                UPDATE roles
                SET description = COALESCE(NULLIF(description, ''), :description),
                    is_active = true,
                    updated_at = :now
                WHERE id = :id
                """
            ),
            {"id": role_id, "description": ROLE_DESCRIPTION, "now": now},
        )

    bind.execute(
        sa.text(
            """
            DELETE FROM role_permissions rp
            USING permissions p
            WHERE rp.role_id = CAST(:role_id AS uuid)
              AND rp.permission_id = p.id
              AND p.key LIKE :network_any_pattern
              AND p.key <> 'network:read'
              AND p.key <> 'network:ont:write'
              AND p.key NOT LIKE :network_read_pattern
            """
        ),
        {
            "role_id": str(role_id),
            "network_any_pattern": "network:%",
            "network_read_pattern": "network:%:read",
        },
    )

    bind.execute(
        sa.text(
            """
            INSERT INTO role_permissions (id, role_id, permission_id)
            SELECT (
                   substr(md5(CAST(:role_id AS text) || p.id::text), 1, 8) || '-' ||
                   substr(md5(CAST(:role_id AS text) || p.id::text), 9, 4) || '-' ||
                   substr(md5(CAST(:role_id AS text) || p.id::text), 13, 4) || '-' ||
                   substr(md5(CAST(:role_id AS text) || p.id::text), 17, 4) || '-' ||
                   substr(md5(CAST(:role_id AS text) || p.id::text), 21, 12)
                   )::uuid,
                   CAST(:role_id AS uuid),
                   p.id
            FROM permissions p
            WHERE p.is_active = true
              AND (
                  p.key = 'network:read'
                  OR p.key = 'network:ont:write'
                  OR p.key LIKE :network_read_pattern
              )
            ON CONFLICT (role_id, permission_id) DO NOTHING
            """
        ),
        {"role_id": str(role_id), "network_read_pattern": "network:%:read"},
    )


def downgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    if not {"roles", "permissions", "role_permissions"}.issubset(
        inspector.get_table_names()
    ):
        return

    role_id = bind.execute(
        sa.text("SELECT id FROM roles WHERE name = :name"),
        {"name": ROLE_NAME},
    ).scalar()
    if role_id is None:
        return

    bind.execute(
        sa.text(
            """
            DELETE FROM role_permissions rp
            USING permissions p
            WHERE rp.role_id = CAST(:role_id AS uuid)
              AND rp.permission_id = p.id
              AND p.key LIKE :network_any_pattern
              AND p.key <> 'network:read'
              AND p.key <> 'network:ont:read'
              AND p.key <> 'network:ont:write'
            """
        ),
        {"role_id": str(role_id), "network_any_pattern": "network:%"},
    )
