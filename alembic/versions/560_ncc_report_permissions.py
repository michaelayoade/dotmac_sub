"""Add explicit NCC report permissions.

Revision ID: 560_ncc_report_permissions
Revises: 559_upcoming_charges_indexes
Create Date: 2026-08-28
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import UTC, datetime
from uuid import uuid4

import sqlalchemy as sa

from alembic import op

revision: str = "560_ncc_report_permissions"
down_revision: str | None = "559_upcoming_charges_indexes"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

PERMISSIONS = (
    ("reports:ncc:read", "View NCC regulatory reports"),
    ("reports:ncc:export", "Export NCC regulatory report data and artifacts"),
)

CUSTOMER_EXPERIENCE_ROLE_NAMES = (
    "Customer experience",
    "Customer experience managers",
    "customer_experience",
    "customer_experience_manager",
    "customer_experience_managers",
)

GRANT_TABLES = (
    "role_permissions",
    "subscriber_permissions",
    "system_user_permissions",
)


def _permission_id(bind, key: str) -> str | None:
    return bind.execute(
        sa.text("SELECT id FROM permissions WHERE key = :key"), {"key": key}
    ).scalar()


def _ensure_permission(bind, *, key: str, description: str, now: datetime) -> str:
    permission_id = _permission_id(bind, key)
    if permission_id is not None:
        bind.execute(
            sa.text(
                """
                UPDATE permissions
                SET description = COALESCE(NULLIF(description, ''), :description),
                    is_active = true,
                    is_ui_assignable = true,
                    updated_at = :now
                WHERE id = :id
                """
            ),
            {"id": permission_id, "description": description, "now": now},
        )
        return permission_id

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
        {"id": permission_id, "key": key, "description": description, "now": now},
    )
    return permission_id


def _grant_permission_to_customer_experience_roles(
    bind, *, tables: set[str], permission_id: str
) -> None:
    if not {"roles", "role_permissions"}.issubset(tables):
        return
    role_ids = [
        row[0]
        for row in bind.execute(
            sa.text("SELECT id FROM roles WHERE name IN :names").bindparams(
                sa.bindparam("names", expanding=True)
            ),
            {"names": CUSTOMER_EXPERIENCE_ROLE_NAMES},
        ).fetchall()
    ]
    for role_id in role_ids:
        already = bind.execute(
            sa.text(
                "SELECT 1 FROM role_permissions "
                "WHERE role_id = :role_id AND permission_id = :permission_id"
            ),
            {"role_id": role_id, "permission_id": permission_id},
        ).scalar()
        if already:
            continue
        bind.execute(
            sa.text(
                "INSERT INTO role_permissions (id, role_id, permission_id) "
                "VALUES (:id, :role_id, :permission_id)"
            ),
            {"id": str(uuid4()), "role_id": role_id, "permission_id": permission_id},
        )


def upgrade() -> None:
    bind = op.get_bind()
    tables = set(sa.inspect(bind).get_table_names())
    if "permissions" not in tables:
        return
    now = datetime.now(UTC)

    for key, description in PERMISSIONS:
        permission_id = _ensure_permission(
            bind, key=key, description=description, now=now
        )
        _grant_permission_to_customer_experience_roles(
            bind, tables=tables, permission_id=permission_id
        )


def downgrade() -> None:
    bind = op.get_bind()
    tables = set(sa.inspect(bind).get_table_names())
    if "permissions" not in tables:
        return
    for key, _description in PERMISSIONS:
        permission_id = _permission_id(bind, key)
        if permission_id is None:
            continue
        for table in GRANT_TABLES:
            if table in tables:
                bind.execute(
                    sa.text("DELETE FROM " + table + " WHERE permission_id = :id"),
                    {"id": permission_id},
                )
        bind.execute(
            sa.text("DELETE FROM permissions WHERE id = :id"),
            {"id": permission_id},
        )
