"""Add customer.account_recovery permissions, seeded only to the admin role.

Revision ID: 613_account_recovery_permissions
Revises: 612_account_recovery_evidence
Create Date: 2026-09-13
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import UTC, datetime
from uuid import uuid4

import sqlalchemy as sa

from alembic import op

revision: str = "613_account_recovery_permissions"
down_revision: str | None = "612_account_recovery_evidence"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

PERMISSIONS = (
    (
        "customer:account_recovery:read",
        "View accounts with an open customer.account_recovery generation",
    ),
    (
        "customer:account_recovery:restore",
        "Restore an account through customer.account_recovery",
    ),
    (
        "customer:account_recovery:rebaseline",
        "Re-baseline customer.account_recovery evidence for one generation",
    ),
)

# Seeded ONLY to the admin role — no non-admin role is granted these keys.
TARGET_ROLE_NAMES = ("admin",)


def _permission_id(bind, key: str) -> str | None:
    return bind.execute(
        sa.text("SELECT id FROM permissions WHERE key = :key"), {"key": key}
    ).scalar()


def upgrade() -> None:
    bind = op.get_bind()
    tables = sa.inspect(bind).get_table_names()
    if "permissions" not in tables:
        return

    now = datetime.now(UTC)
    permission_ids: list[str] = []
    for key, description in PERMISSIONS:
        permission_id = _permission_id(bind, key)
        if permission_id is None:
            permission_id = str(uuid4())
            bind.execute(
                sa.text(
                    """
                    INSERT INTO permissions (
                        id, key, description, is_active, is_ui_assignable,
                        created_at, updated_at
                    )
                    VALUES (:id, :key, :description, true, false, :now, :now)
                    """
                ),
                {
                    "id": permission_id,
                    "key": key,
                    "description": description,
                    "now": now,
                },
            )
        permission_ids.append(permission_id)

    if "role_permissions" not in tables or "roles" not in tables:
        return

    role_ids = [
        row[0]
        for row in bind.execute(
            sa.text("SELECT id FROM roles WHERE name IN :names").bindparams(
                sa.bindparam("names", expanding=True)
            ),
            {"names": TARGET_ROLE_NAMES},
        ).fetchall()
    ]
    for role_id in role_ids:
        for permission_id in permission_ids:
            already = bind.execute(
                sa.text(
                    "SELECT 1 FROM role_permissions "
                    "WHERE role_id = :role_id AND permission_id = :permission_id"
                ),
                {"role_id": role_id, "permission_id": permission_id},
            ).scalar()
            if not already:
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
    tables = sa.inspect(bind).get_table_names()
    if "permissions" not in tables:
        return
    keys = [key for key, _ in PERMISSIONS]
    permission_ids = [
        pid for key in keys if (pid := _permission_id(bind, key)) is not None
    ]
    if not permission_ids:
        return
    if "role_permissions" in tables:
        bind.execute(
            sa.text(
                "DELETE FROM role_permissions WHERE permission_id IN :ids"
            ).bindparams(sa.bindparam("ids", expanding=True)),
            {"ids": permission_ids},
        )
    bind.execute(
        sa.text("DELETE FROM permissions WHERE key IN :keys").bindparams(
            sa.bindparam("keys", expanding=True)
        ),
        {"keys": keys},
    )
