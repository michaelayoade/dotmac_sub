"""Add narrow customer communication send permissions.

Revision ID: 561_customer_comm_send_perms
Revises: 560_oidc_mobile_federation
Create Date: 2026-08-27
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import UTC, datetime
from uuid import uuid4

import sqlalchemy as sa

from alembic import op

revision: str = "561_customer_comm_send_perms"
down_revision: str | None = "560_oidc_mobile_federation"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

PERMISSIONS = (
    (
        "monitoring:outage_notify:send",
        "Send outage notifications to affected customers",
        "monitoring:write",
    ),
    (
        "communications:customer:send",
        "Send customer notifications to selected customer scopes",
        "customer:write",
    ),
)

CUSTOMER_EXPERIENCE_ROLE_NAMES = (
    "Customer experience",
    "Customer experience managers",
    "customer_experience",
    "customer_experience_manager",
    "customer_experience_managers",
)

GRANT_TABLES = (
    ("role_permissions", "role_id", None),
    ("subscriber_permissions", "subscriber_id", "granted_by_subscriber_id"),
    ("system_user_permissions", "system_user_id", "granted_by_system_user_id"),
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


def _copy_holder_grants(
    bind,
    *,
    tables: set[str],
    table: str,
    holder_column: str,
    granted_by_column: str | None,
    source_id: str,
    target_id: str,
    now: datetime,
) -> None:
    if table not in tables:
        return
    extra_columns = f", granted_at, {granted_by_column}" if granted_by_column else ""
    rows = bind.execute(
        sa.text(
            f"SELECT {holder_column}{extra_columns} "
            f"FROM {table} WHERE permission_id = :permission_id"
        ),
        {"permission_id": source_id},
    ).fetchall()
    for row in rows:
        holder_id = row[0]
        already = bind.execute(
            sa.text(
                f"SELECT 1 FROM {table} "
                f"WHERE {holder_column} = :holder_id "
                "AND permission_id = :permission_id"
            ),
            {"holder_id": holder_id, "permission_id": target_id},
        ).scalar()
        if already:
            continue
        if granted_by_column:
            bind.execute(
                sa.text(
                    f"INSERT INTO {table} "
                    f"(id, {holder_column}, permission_id, granted_at, "
                    f"{granted_by_column}) "
                    "VALUES (:id, :holder_id, :permission_id, :granted_at, "
                    ":granted_by)"
                ),
                {
                    "id": str(uuid4()),
                    "holder_id": holder_id,
                    "permission_id": target_id,
                    "granted_at": row[1] or now,
                    "granted_by": row[2],
                },
            )
        else:
            bind.execute(
                sa.text(
                    f"INSERT INTO {table} "
                    f"(id, {holder_column}, permission_id) "
                    "VALUES (:id, :holder_id, :permission_id)"
                ),
                {
                    "id": str(uuid4()),
                    "holder_id": holder_id,
                    "permission_id": target_id,
                },
            )


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

    for key, description, source_key in PERMISSIONS:
        permission_id = _ensure_permission(
            bind, key=key, description=description, now=now
        )
        source_id = _permission_id(bind, source_key)
        if source_id is not None:
            for table, holder_column, granted_by_column in GRANT_TABLES:
                _copy_holder_grants(
                    bind,
                    tables=tables,
                    table=table,
                    holder_column=holder_column,
                    granted_by_column=granted_by_column,
                    source_id=source_id,
                    target_id=permission_id,
                    now=now,
                )
        _grant_permission_to_customer_experience_roles(
            bind, tables=tables, permission_id=permission_id
        )


def downgrade() -> None:
    bind = op.get_bind()
    tables = set(sa.inspect(bind).get_table_names())
    if "permissions" not in tables:
        return
    for key, _description, _source_key in PERMISSIONS:
        permission_id = _permission_id(bind, key)
        if permission_id is None:
            continue
        for table, _holder_column, _granted_by_column in GRANT_TABLES:
            if table in tables:
                bind.execute(
                    sa.text("DELETE FROM " + table + " WHERE permission_id = :id"),
                    {"id": permission_id},
                )
        bind.execute(
            sa.text("DELETE FROM permissions WHERE id = :id"),
            {"id": permission_id},
        )
