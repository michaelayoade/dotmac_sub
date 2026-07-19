"""Retire the coarse reports:billing / reports:network permissions.

The admin reports routes now declare ``reports:billing:read`` / ``:export`` and
``reports:network:read`` / ``:export``, so the coarse ``reports:billing`` and
``reports:network`` permissions have no remaining consumer. Migration 370 already
granted the granular permissions to every role and directly granted principal
that held a coarse one, so deleting them here removes no access. The downgrade
reconstructs coarse grants from granular holders before migration 370 removes the
granular keys.

Revision ID: 371_retire_coarse_reports_permissions
Revises: 370_reports_granular_permissions
"""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import uuid4

import sqlalchemy as sa

from alembic import op

revision = "371_retire_coarse_reports_permissions"
down_revision = "370_reports_granular_permissions"
branch_labels = None
depends_on = None

COARSE = {
    "reports:billing": "Billing and revenue reports",
    "reports:network": "Network and bandwidth reports",
}

COARSE_TO_GRANULAR_KEYS = {
    "reports:billing": ("reports:billing:read", "reports:billing:export"),
    "reports:network": ("reports:network:read", "reports:network:export"),
}

_GRANT_TABLES = (
    ("role_permissions", "role_id", None),
    ("subscriber_permissions", "subscriber_id", "granted_by_subscriber_id"),
    ("system_user_permissions", "system_user_id", "granted_by_system_user_id"),
)


def _permission_id(bind, key: str):
    return bind.execute(
        sa.text("SELECT id FROM permissions WHERE key = :key"), {"key": key}
    ).scalar()


def _delete_grants(bind, *, tables: set[str], permission_id) -> None:
    for table, _holder_column, _granted_by_column in _GRANT_TABLES:
        if table in tables:
            bind.execute(
                sa.text(f"DELETE FROM {table} WHERE permission_id = :permission_id"),
                {"permission_id": permission_id},
            )


def _restore_holder_grants(
    bind,
    *,
    tables: set[str],
    table: str,
    holder_column: str,
    granted_by_column: str | None,
    granular_ids: list,
    coarse_id,
    now: datetime,
) -> None:
    if table not in tables or not granular_ids:
        return
    extra_columns = f", granted_at, {granted_by_column}" if granted_by_column else ""
    rows = bind.execute(
        sa.text(
            f"SELECT {holder_column}{extra_columns} FROM {table} "
            "WHERE permission_id IN :permission_ids"
        ).bindparams(sa.bindparam("permission_ids", expanding=True)),
        {"permission_ids": granular_ids},
    ).fetchall()
    restored: set[object] = set()
    for row in rows:
        holder_id = row[0]
        if holder_id in restored:
            continue
        restored.add(holder_id)
        already = bind.execute(
            sa.text(
                f"SELECT 1 FROM {table} WHERE {holder_column} = :holder_id "
                "AND permission_id = :permission_id"
            ),
            {"holder_id": holder_id, "permission_id": coarse_id},
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
                    "permission_id": coarse_id,
                    "granted_at": row[1] or now,
                    "granted_by": row[2],
                },
            )
        else:
            bind.execute(
                sa.text(
                    f"INSERT INTO {table} (id, {holder_column}, permission_id) "
                    "VALUES (:id, :holder_id, :permission_id)"
                ),
                {
                    "id": str(uuid4()),
                    "holder_id": holder_id,
                    "permission_id": coarse_id,
                },
            )


def upgrade() -> None:
    bind = op.get_bind()
    tables = set(sa.inspect(bind).get_table_names())
    if "permissions" not in tables:
        return
    for key in COARSE:
        pid = _permission_id(bind, key)
        if not pid:
            continue
        _delete_grants(bind, tables=tables, permission_id=pid)
        bind.execute(sa.text("DELETE FROM permissions WHERE id = :p"), {"p": pid})


def downgrade() -> None:
    bind = op.get_bind()
    tables = set(sa.inspect(bind).get_table_names())
    if "permissions" not in tables:
        return
    now = datetime.now(UTC)
    for key, description in COARSE.items():
        coarse_id = _permission_id(bind, key)
        if not coarse_id:
            coarse_id = str(uuid4())
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
                    "id": coarse_id,
                    "key": key,
                    "description": description,
                    "now": now,
                },
            )
        granular_ids = [
            permission_id
            for granular_key in COARSE_TO_GRANULAR_KEYS[key]
            if (permission_id := _permission_id(bind, granular_key))
        ]
        for table, holder_column, granted_by_column in _GRANT_TABLES:
            _restore_holder_grants(
                bind,
                tables=tables,
                table=table,
                holder_column=holder_column,
                granted_by_column=granted_by_column,
                granular_ids=granular_ids,
                coarse_id=coarse_id,
                now=now,
            )
