"""Grant ticket assignment to the authoritative support roles.

Revision ID: 572_ticket_assignment_role_grants
Revises: 571_seed_workqueue_audience_permissions
Create Date: 2026-09-02

The permission already exists. This migration only reconciles the two
checked-in support roles that are intended to assign tickets. It does not infer
or mutate deployment-specific Project Manager roles.
"""

from __future__ import annotations

from collections.abc import Sequence
from uuid import uuid4

import sqlalchemy as sa

from alembic import op

revision: str = "572_ticket_assignment_role_grants"
down_revision: str | None = "571_seed_workqueue_audience_permissions"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

PERMISSION_KEY = "support:ticket:assign"
ROLE_NAMES = ("support", "Technical support")


def upgrade() -> None:
    bind = op.get_bind()
    tables = set(sa.inspect(bind).get_table_names())
    if not {"roles", "permissions", "role_permissions"}.issubset(tables):
        return

    permission_id = bind.execute(
        sa.text("SELECT id FROM permissions WHERE key = :key AND is_active = true"),
        {"key": PERMISSION_KEY},
    ).scalar()
    if permission_id is None:
        return

    normalized_names = tuple(name.casefold() for name in ROLE_NAMES)
    role_ids = tuple(
        bind.execute(
            sa.text(
                "SELECT id FROM roles "
                "WHERE lower(trim(name)) IN :names AND is_active = true"
            ).bindparams(sa.bindparam("names", expanding=True)),
            {"names": normalized_names},
        ).scalars()
    )
    for role_id in role_ids:
        already_granted = bind.execute(
            sa.text(
                "SELECT 1 FROM role_permissions "
                "WHERE role_id = :role_id AND permission_id = :permission_id"
            ),
            {"role_id": role_id, "permission_id": permission_id},
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
                "permission_id": permission_id,
            },
        )


def downgrade() -> None:
    bind = op.get_bind()
    tables = set(sa.inspect(bind).get_table_names())
    if not {"roles", "permissions", "role_permissions"}.issubset(tables):
        return

    normalized_names = tuple(name.casefold() for name in ROLE_NAMES)
    bind.execute(
        sa.text(
            """
            DELETE FROM role_permissions rp
            USING roles r, permissions p
            WHERE rp.role_id = r.id
              AND rp.permission_id = p.id
              AND lower(trim(r.name)) IN :names
              AND p.key = :key
            """
        ).bindparams(sa.bindparam("names", expanding=True)),
        {"names": normalized_names, "key": PERMISSION_KEY},
    )
