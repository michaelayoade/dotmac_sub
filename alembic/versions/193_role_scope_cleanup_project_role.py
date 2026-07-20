"""Tighten support billing access and add Project role.

Revision ID: 193_role_scope_cleanup_project_role
Revises: 192_add_subscription_write_permission
"""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import uuid4

import sqlalchemy as sa

from alembic import op

revision = "193_role_scope_cleanup_project_role"
down_revision = "192_add_subscription_write_permission"
branch_labels = None
depends_on = None


PROJECT_ROLE_NAME = "Project"
PROJECT_ROLE_DESCRIPTION = (
    "Project read-only access to customers, network, projects, and tickets"
)

PERMISSIONS: dict[str, str] = {
    "customer:read": "View customers and subscribers",
    "monitoring:read": "View monitoring dashboards and alerts",
    "network:authorization:read": "View network authorization presets",
    "network:cpe:read": "View CPE devices",
    "network:device:read": "View network devices",
    "network:dns_threat:read": "View DNS threat monitoring",
    "network:fiber:read": "View fiber infrastructure",
    "network:hub:read": "View the network operations hub",
    "network:ip:read": "View IP pools and assignments",
    "network:map:read": "View the comprehensive network map",
    "network:nas:read": "View NAS device management",
    "network:olt:read": "View OLT devices and operations",
    "network:ont:read": "View ONT units",
    "network:onu_type:read": "View ONU type catalog",
    "network:pon:read": "View PON interfaces",
    "network:pop:read": "View network POP sites",
    "network:radius:read": "View RADIUS configuration",
    "network:read": "Read network inventory and telemetry",
    "network:speed_profile:read": "View network speed profiles",
    "network:speedtest:read": "View network speed tests",
    "network:tr069:read": "View TR-069 / ACS management",
    "network:vendor_capability:read": "View network vendor capabilities",
    "network:vpn:read": "View VPN infrastructure and tunnels",
    "network:weathermap:read": "View network weathermap",
    "network:zone:read": "View network zones",
    "project:read": "View projects",
    "project:task:read": "View project tasks",
    "support:ticket:read": "View tickets",
}

ROLE_BILLING_RESTORE_KEYS: dict[str, tuple[str, ...]] = {
    "Customer experience": (
        "billing:account:read",
        "billing:arrangement:read",
        "billing:batch:read",
        "billing:credit_note:read",
        "billing:dunning:read",
        "billing:extension:create",
        "billing:extension:read",
        "billing:invoice:create",
        "billing:invoice:read",
        "billing:ledger:read",
        "billing:payment:create",
        "billing:payment:read",
        "billing:proof:read",
        "billing:proof:verify",
        "billing:tax:read",
        "billing:vas:read",
        "billing_account:read",
    ),
    "Technical support": (
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
    ),
}


def _ensure_permission(bind, *, key: str, description: str, now: datetime) -> None:
    existing = bind.execute(
        sa.text("SELECT id FROM permissions WHERE key = :key"),
        {"key": key},
    ).scalar()
    if existing:
        bind.execute(
            sa.text(
                """
                UPDATE permissions
                SET description = COALESCE(NULLIF(description, ''), :description),
                    is_active = true,
                    updated_at = :now
                WHERE key = :key
                """
            ),
            {"key": key, "description": description, "now": now},
        )
        return

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
            "key": key,
            "description": description,
            "now": now,
        },
    )


def _ensure_role(bind, *, name: str, description: str, now: datetime) -> str:
    role_id = bind.execute(
        sa.text("SELECT id FROM roles WHERE name = :name"),
        {"name": name},
    ).scalar()
    if role_id:
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
            {"id": role_id, "description": description, "now": now},
        )
        return str(role_id)

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
            "name": name,
            "description": description,
            "now": now,
        },
    )
    return role_id


def _grant_permissions(
    bind, *, role_name: str, permission_keys: tuple[str, ...]
) -> None:
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
            JOIN permissions p ON p.key = ANY(:permission_keys)
            WHERE r.name = :role_name
              AND r.is_active = true
              AND p.is_active = true
            ON CONFLICT (role_id, permission_id) DO NOTHING
            """
        ),
        {"role_name": role_name, "permission_keys": list(permission_keys)},
    )


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    if not {"roles", "permissions", "role_permissions"}.issubset(
        inspector.get_table_names()
    ):
        return

    now = datetime.now(UTC)
    for key, description in PERMISSIONS.items():
        _ensure_permission(bind, key=key, description=description, now=now)

    project_role_id = _ensure_role(
        bind,
        name=PROJECT_ROLE_NAME,
        description=PROJECT_ROLE_DESCRIPTION,
        now=now,
    )
    _grant_permissions(
        bind,
        role_name=PROJECT_ROLE_NAME,
        permission_keys=tuple(PERMISSIONS),
    )

    bind.execute(
        sa.text(
            """
            DELETE FROM role_permissions rp
            USING roles r, permissions p
            WHERE rp.role_id = r.id
              AND rp.permission_id = p.id
              AND r.name = ANY(:role_names)
              AND (
                  p.key LIKE 'billing:%'
                  OR p.key LIKE 'billing_account:%'
              )
            """
        ),
        {"role_names": ["Customer experience", "Technical support"]},
    )

    bind.execute(
        sa.text("UPDATE roles SET updated_at = :now WHERE id = CAST(:id AS uuid)"),
        {"id": project_role_id, "now": now},
    )


def downgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    if not {"roles", "permissions", "role_permissions"}.issubset(
        inspector.get_table_names()
    ):
        return

    for role_name, permission_keys in ROLE_BILLING_RESTORE_KEYS.items():
        _grant_permissions(bind, role_name=role_name, permission_keys=permission_keys)

    role_id = bind.execute(
        sa.text("SELECT id FROM roles WHERE name = :name"),
        {"name": PROJECT_ROLE_NAME},
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
