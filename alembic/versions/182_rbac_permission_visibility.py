"""Hide admin-only RBAC permissions from role builders.

Revision ID: 182_rbac_permission_visibility
Revises: 181_fk_ondelete_hardening
"""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import uuid4

import sqlalchemy as sa

from alembic import op

revision = "182_rbac_permission_visibility"
down_revision = "181_fk_ondelete_hardening"
branch_labels = None
depends_on = None

ADMIN_ONLY_PERMISSION_KEYS = {
    "*",
    "auth:manage",
    "billing:read",
    "billing:write",
    "catalog:read",
    "catalog:write",
    "network:read",
    "network:write",
    "provisioning:read",
    "provisioning:write",
    "rbac:assign",
    "rbac:permissions:delete",
    "rbac:permissions:read",
    "rbac:permissions:write",
    "rbac:roles:delete",
    "rbac:roles:read",
    "rbac:roles:write",
    "subscriber:impersonate",
    "subscriber:read",
    "subscriber:write",
    "system:settings:read",
    "system:settings:write",
}

NEW_PERMISSIONS = {
    "billing:proof:read": "View submitted payment proofs",
    "billing:proof:verify": "Verify or reject submitted payment proofs",
    "billing:vas:read": "View value-added-service administration",
    "billing:vas:write": "Manage value-added-service administration",
    "billing:ledger:write": "Manage ledger entries",
    "network:authorization:read": "View network authorization presets",
    "network:authorization:write": "Manage network authorization presets",
    "network:cpe:read": "View CPE devices",
    "network:cpe:write": "Manage CPE devices",
    "network:olt:read": "View OLT devices and operations",
    "network:olt:write": "Manage OLT devices and operations",
    "network:onu_type:read": "View ONU type catalog",
    "network:onu_type:write": "Manage ONU type catalog",
    "network:pon:read": "View PON interfaces",
    "network:pon:write": "Manage PON interfaces",
    "network:pop:read": "View network POP sites",
    "network:pop:write": "Manage network POP sites",
    "network:speed_profile:read": "View network speed profiles",
    "network:speed_profile:write": "Manage network speed profiles",
    "network:vendor_capability:read": "View network vendor capabilities",
    "network:vendor_capability:write": "Manage network vendor capabilities",
    "network:weathermap:read": "View network weathermap",
    "network:weathermap:write": "Manage network weathermap",
    "network:zone:read": "View network zones",
    "network:zone:write": "Manage network zones",
}


def _has_column(table: str, column: str) -> bool:
    bind = op.get_bind()
    return column in {col["name"] for col in sa.inspect(bind).get_columns(table)}


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    if "permissions" not in inspector.get_table_names():
        return
    permissions_table = sa.table(
        "permissions",
        sa.column("id"),
        sa.column("key"),
        sa.column("is_ui_assignable"),
        sa.column("updated_at"),
    )

    if not _has_column("permissions", "is_ui_assignable"):
        op.add_column(
            "permissions",
            sa.Column(
                "is_ui_assignable",
                sa.Boolean(),
                nullable=False,
                server_default=sa.true(),
            ),
        )

    now = datetime.now(UTC)
    for key, description in NEW_PERMISSIONS.items():
        existing = bind.execute(
            sa.text("SELECT id FROM permissions WHERE key = :key"),
            {"key": key},
        ).scalar()
        if existing:
            bind.execute(
                sa.text(
                    """
                    UPDATE permissions
                    SET description = COALESCE(description, :description),
                        is_active = true,
                        is_ui_assignable = true,
                        updated_at = :now
                    WHERE key = :key
                    """
                ),
                {"key": key, "description": description, "now": now},
            )
        else:
            bind.execute(
                sa.text(
                    """
                    INSERT INTO permissions (
                        id, key, description, is_active, is_ui_assignable,
                        created_at, updated_at
                    )
                    VALUES (
                        :id, :key, :description, true, true, :now, :now
                    )
                    """
                ),
                {
                    "id": str(uuid4()),
                    "key": key,
                    "description": description,
                    "now": now,
                },
            )

    bind.execute(
        sa.update(permissions_table).values(
            is_ui_assignable=sa.case(
                (permissions_table.c.key.in_(ADMIN_ONLY_PERMISSION_KEYS), False),
                else_=True,
            ),
            updated_at=now,
        )
    )

    if (
        "role_permissions" in inspector.get_table_names()
        and "roles" in inspector.get_table_names()
    ):
        role_permissions_table = sa.table(
            "role_permissions",
            sa.column("role_id"),
            sa.column("permission_id"),
        )
        roles_table = sa.table("roles", sa.column("id"), sa.column("name"))
        hidden_permission_ids = [
            row[0]
            for row in bind.execute(
                sa.select(permissions_table.c.id).where(
                    permissions_table.c.key.in_(ADMIN_ONLY_PERMISSION_KEYS)
                )
            )
        ]
        non_admin_role_ids = [
            row[0]
            for row in bind.execute(
                sa.select(roles_table.c.id).where(roles_table.c.name != "admin")
            )
        ]
        if hidden_permission_ids and non_admin_role_ids:
            bind.execute(
                sa.delete(role_permissions_table)
                .where(
                    role_permissions_table.c.permission_id.in_(hidden_permission_ids)
                )
                .where(role_permissions_table.c.role_id.in_(non_admin_role_ids))
            )


def downgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    if "permissions" not in inspector.get_table_names():
        return

    permissions_table = sa.table("permissions", sa.column("key"))
    bind.execute(
        sa.delete(permissions_table).where(
            permissions_table.c.key.in_(set(NEW_PERMISSIONS))
        )
    )
    if _has_column("permissions", "is_ui_assignable"):
        op.drop_column("permissions", "is_ui_assignable")
