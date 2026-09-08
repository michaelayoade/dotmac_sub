"""Remove dedicated ticket assignment permission.

Revision ID: 574_remove_ticket_assignment_permission
Revises: 573_ticket_assignment_role_grants
Create Date: 2026-09-03

Ticket assignment is restored to the existing support:ticket:update authority.
This removes the separate support:ticket:assign grants and permission row that
were introduced for the split assignment policy.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import UTC, datetime
from uuid import uuid4

import sqlalchemy as sa

from alembic import op

revision: str = "574_remove_ticket_assignment_permission"
down_revision: str | None = "573_ticket_assignment_role_grants"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

UPDATE_PERMISSION_KEY = "support:ticket:update"
ASSIGN_PERMISSION_KEY = "support:ticket:assign"
EXCLUDED_ROLE_NAME = "project_management_office"
GRANT_TABLES = (
    "role_permissions",
    "subscriber_permissions",
    "system_user_permissions",
)


def _permission_id(bind: sa.Connection, key: str) -> object | None:
    return bind.execute(
        sa.text("SELECT id FROM permissions WHERE key = :key"), {"key": key}
    ).scalar()


def upgrade() -> None:
    bind = op.get_bind()
    tables = set(sa.inspect(bind).get_table_names())
    if "permissions" not in tables:
        return

    permission_id = _permission_id(bind, ASSIGN_PERMISSION_KEY)
    if permission_id is None:
        return

    for table in GRANT_TABLES:
        if table in tables:
            bind.execute(
                sa.text(f"DELETE FROM {table} WHERE permission_id = :permission_id"),
                {"permission_id": permission_id},
            )
    bind.execute(
        sa.text("DELETE FROM permissions WHERE id = :permission_id"),
        {"permission_id": permission_id},
    )


def downgrade() -> None:
    bind = op.get_bind()
    tables = set(sa.inspect(bind).get_table_names())
    if not {"roles", "permissions", "role_permissions"}.issubset(tables):
        return

    permission_id = _permission_id(bind, ASSIGN_PERMISSION_KEY)
    if permission_id is None:
        permission_id = str(uuid4())
        now = datetime.now(UTC)
        bind.execute(
            sa.text(
                "INSERT INTO permissions ("
                "id, key, description, is_active, is_ui_assignable, "
                "created_at, updated_at"
                ") VALUES (:id, :key, :description, true, true, :now, :now)"
            ),
            {
                "id": permission_id,
                "key": ASSIGN_PERMISSION_KEY,
                "description": "Assign tickets",
                "now": now,
            },
        )

    update_permission_id = _permission_id(bind, UPDATE_PERMISSION_KEY)
    if update_permission_id is None:
        return

    role_ids = tuple(
        bind.execute(
            sa.text(
                "SELECT DISTINCT r.id FROM roles r "
                "JOIN role_permissions rp ON rp.role_id = r.id "
                "WHERE rp.permission_id = :update_permission_id "
                "AND r.is_active = true "
                "AND lower(trim(r.name)) != :excluded_role_name"
            ),
            {
                "update_permission_id": update_permission_id,
                "excluded_role_name": EXCLUDED_ROLE_NAME.casefold(),
            },
        ).scalars()
    )
    for role_id in role_ids:
        existing = bind.execute(
            sa.text(
                "SELECT 1 FROM role_permissions "
                "WHERE role_id = :role_id AND permission_id = :permission_id"
            ),
            {"role_id": role_id, "permission_id": permission_id},
        ).scalar()
        if existing:
            continue
        bind.execute(
            sa.text(
                "INSERT INTO role_permissions (id, role_id, permission_id) "
                "VALUES (:id, :role_id, :permission_id)"
            ),
            {
                "id": str(uuid4()),
                "role_id": role_id,
                "permission_id": permission_id,
            },
        )
