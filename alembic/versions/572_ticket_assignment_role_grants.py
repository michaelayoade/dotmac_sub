"""Grant ticket assignment to ticket-update roles except Project Management.

Revision ID: 572_ticket_assignment_role_grants
Revises: 571_seed_workqueue_audience_permissions
Create Date: 2026-09-02

The permission already exists. This migration reconciles every active role
that currently holds ``support:ticket:update`` while explicitly excluding the
authoritative ``project_management_office`` role.
"""

from __future__ import annotations

from collections.abc import Sequence
from uuid import uuid4

import sqlalchemy as sa

from alembic import op

revision: str = "572_ticket_assignment_role_grants"
down_revision: str | None = "572_erp_staff_access_restrictions"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

UPDATE_PERMISSION_KEY = "support:ticket:update"
ASSIGN_PERMISSION_KEY = "support:ticket:assign"
EXCLUDED_ROLE_NAME = "project_management_office"


def upgrade() -> None:
    bind = op.get_bind()
    tables = set(sa.inspect(bind).get_table_names())
    if not {"roles", "permissions", "role_permissions"}.issubset(tables):
        return

    assign_permission_id = bind.execute(
        sa.text("SELECT id FROM permissions WHERE key = :key AND is_active = true"),
        {"key": ASSIGN_PERMISSION_KEY},
    ).scalar()
    update_permission_id = bind.execute(
        sa.text("SELECT id FROM permissions WHERE key = :key AND is_active = true"),
        {"key": UPDATE_PERMISSION_KEY},
    ).scalar()
    if assign_permission_id is None or update_permission_id is None:
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

    excluded_role_ids = tuple(
        bind.execute(
            sa.text(
                "SELECT id FROM roles WHERE lower(trim(name)) = :excluded_role_name"
            ),
            {"excluded_role_name": EXCLUDED_ROLE_NAME.casefold()},
        ).scalars()
    )
    for role_id in excluded_role_ids:
        bind.execute(
            sa.text(
                "DELETE FROM role_permissions "
                "WHERE role_id = :role_id AND permission_id = :permission_id"
            ),
            {"role_id": role_id, "permission_id": assign_permission_id},
        )

    for role_id in role_ids:
        already_granted = bind.execute(
            sa.text(
                "SELECT 1 FROM role_permissions "
                "WHERE role_id = :role_id AND permission_id = :permission_id"
            ),
            {"role_id": role_id, "permission_id": assign_permission_id},
        ).scalar()
        if already_granted:
            continue
        bind.execute(
            sa.text(
                "INSERT INTO role_permissions (id, role_id, permission_id) "
                "VALUES (:id, :role_id, :permission_id)"
            ),
            {
                "id": str(uuid4()),
                "role_id": role_id,
                "permission_id": assign_permission_id,
            },
        )


def downgrade() -> None:
    bind = op.get_bind()
    tables = set(sa.inspect(bind).get_table_names())
    if not {"roles", "permissions", "role_permissions"}.issubset(tables):
        return

    assign_permission_id = bind.execute(
        sa.text("SELECT id FROM permissions WHERE key = :key"),
        {"key": ASSIGN_PERMISSION_KEY},
    ).scalar()
    update_permission_id = bind.execute(
        sa.text("SELECT id FROM permissions WHERE key = :key"),
        {"key": UPDATE_PERMISSION_KEY},
    ).scalar()
    if assign_permission_id is None or update_permission_id is None:
        return

    bind.execute(
        sa.text(
            """
            DELETE FROM role_permissions
            WHERE permission_id = :assign_permission_id
              AND role_id IN (
                  SELECT r.id
                  FROM roles r
                  JOIN role_permissions update_grant
                    ON update_grant.role_id = r.id
                  WHERE update_grant.permission_id = :update_permission_id
                    AND lower(trim(r.name)) != :excluded_role_name
              )
            """
        ),
        {
            "assign_permission_id": assign_permission_id,
            "update_permission_id": update_permission_id,
            "excluded_role_name": EXCLUDED_ROLE_NAME.casefold(),
        },
    )
