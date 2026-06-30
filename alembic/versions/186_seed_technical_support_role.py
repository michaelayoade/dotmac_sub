"""Seed technical support role.

Revision ID: 186_seed_technical_support_role
Revises: 185_router_rest_api_username_width
"""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import uuid4

import sqlalchemy as sa

from alembic import op

revision = "186_seed_technical_support_role"
down_revision = "185_router_rest_api_username_width"
branch_labels = None
depends_on = None


ROLE_NAME = "Technical support"
ROLE_DESCRIPTION = (
    "Customer support access with network read and ONT management permissions"
)
PERMISSION_KEYS = {
    "billing:account:read",
    "billing:arrangement:read",
    "billing:batch:read",
    "billing:credit_note:create",
    "billing:credit_note:read",
    "billing:credit_note:update",
    "billing:dunning:read",
    "billing:extension:apply",
    "billing:extension:create",
    "billing:extension:read",
    "billing:invoice:create",
    "billing:invoice:read",
    "billing:invoice:update",
    "billing:ledger:read",
    "billing:payment:create",
    "billing:payment:read",
    "billing:payment:update",
    "billing:proof:read",
    "billing:proof:verify",
    "billing:tax:read",
    "billing:vas:read",
    "billing:vas:write",
    "catalog:offer:read",
    "catalog:product:read",
    "catalog:read",
    "crm:contact:read",
    "crm:conversation:read",
    "crm:conversation:write",
    "crm:lead:read",
    "customer:impersonate",
    "customer:read",
    "customer:update",
    "customer:write",
    "network:fiber:read",
    "network:ip:read",
    "network:ont:read",
    "network:ont:write",
    "network:onu_type:read",
    "network:pon:read",
    "network:pop:read",
    "network:radius:read",
    "network:read",
    "network:speed_profile:read",
    "reports:subscribers",
    "subscription:create",
    "subscription:read",
    "subscription:update",
    "support:ticket:assign",
    "support:ticket:create",
    "support:ticket:read",
    "support:ticket:update",
    "usage:read",
    "vendor:project:read",
    "vendor:read",
}


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
            INSERT INTO role_permissions (id, role_id, permission_id)
            SELECT (
                   substr(md5(:role_id || p.id::text), 1, 8) || '-' ||
                   substr(md5(:role_id || p.id::text), 9, 4) || '-' ||
                   substr(md5(:role_id || p.id::text), 13, 4) || '-' ||
                   substr(md5(:role_id || p.id::text), 17, 4) || '-' ||
                   substr(md5(:role_id || p.id::text), 21, 12)
                   )::uuid,
                   CAST(:role_id AS uuid),
                   p.id
            FROM permissions p
            WHERE p.key = ANY(:permission_keys)
              AND p.is_active = true
            ON CONFLICT (role_id, permission_id) DO NOTHING
            """
        ),
        {
            "role_id": role_id,
            "permission_keys": sorted(PERMISSION_KEYS),
        },
    )


def downgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    if not {"roles", "role_permissions"}.issubset(inspector.get_table_names()):
        return

    role_id = bind.execute(
        sa.text("SELECT id FROM roles WHERE name = :name"),
        {"name": ROLE_NAME},
    ).scalar()
    if role_id is None:
        return

    bind.execute(
        sa.text("DELETE FROM role_permissions WHERE role_id = :role_id"),
        {"role_id": role_id},
    )
    bind.execute(
        sa.text(
            """
            DELETE FROM roles r
            WHERE r.id = :role_id
              AND NOT EXISTS (
                  SELECT 1 FROM subscriber_roles sr WHERE sr.role_id = r.id
              )
              AND NOT EXISTS (
                  SELECT 1 FROM system_user_roles sur WHERE sur.role_id = r.id
              )
            """
        ),
        {"role_id": role_id},
    )
