"""Add granular reports permissions and grant them to coarse-permission holders.

The admin reports surface is split from the coarse ``reports:billing`` /
``reports:network`` (which each gated both viewing and CSV export) into
``:read`` / ``:export``. This migration seeds the granular permissions on
existing databases and grants both to every role or directly granted principal
that already holds the matching coarse permission, so no principal loses access.
Migration 371 then retires the coarse keys once the routes declare the granular
ones.

Revision ID: 370_reports_granular_permissions
Revises: 369_vendor_lifecycle_evidence
"""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import uuid4

import sqlalchemy as sa

from alembic import op

revision = "370_reports_granular_permissions"
down_revision = "369_vendor_lifecycle_evidence"
branch_labels = None
depends_on = None

# coarse key -> granular (key, description) it expands into.
COARSE_TO_GRANULAR: dict[str, list[tuple[str, str]]] = {
    "reports:billing": [
        ("reports:billing:read", "View billing and revenue reports"),
        ("reports:billing:export", "Export billing and revenue report data"),
    ],
    "reports:network": [
        ("reports:network:read", "View network and bandwidth reports"),
        ("reports:network:export", "Export network and bandwidth report data"),
    ],
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


def _copy_holder_grants(
    bind,
    *,
    tables: set[str],
    table: str,
    holder_column: str,
    granted_by_column: str | None,
    coarse_id,
    granular_ids: list,
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
        {"permission_id": coarse_id},
    ).fetchall()
    for row in rows:
        holder_id = row[0]
        for granular_id in granular_ids:
            already = bind.execute(
                sa.text(
                    f"SELECT 1 FROM {table} "
                    f"WHERE {holder_column} = :holder_id "
                    "AND permission_id = :permission_id"
                ),
                {"holder_id": holder_id, "permission_id": granular_id},
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
                        "permission_id": granular_id,
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
                        "permission_id": granular_id,
                    },
                )


def upgrade() -> None:
    bind = op.get_bind()
    tables = set(sa.inspect(bind).get_table_names())
    if "permissions" not in tables:
        return
    now = datetime.now(UTC)

    granular_ids: dict[str, str] = {}
    for granular in COARSE_TO_GRANULAR.values():
        for key, description in granular:
            pid = _permission_id(bind, key)
            if not pid:
                pid = str(uuid4())
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
                    {"id": pid, "key": key, "description": description, "now": now},
                )
            granular_ids[key] = pid

    for coarse_key, granular in COARSE_TO_GRANULAR.items():
        coarse_id = _permission_id(bind, coarse_key)
        if not coarse_id:
            continue
        target_ids = [granular_ids[key] for key, _ in granular]
        for table, holder_column, granted_by_column in _GRANT_TABLES:
            _copy_holder_grants(
                bind,
                tables=tables,
                table=table,
                holder_column=holder_column,
                granted_by_column=granted_by_column,
                coarse_id=coarse_id,
                granular_ids=target_ids,
                now=now,
            )


def downgrade() -> None:
    bind = op.get_bind()
    tables = set(sa.inspect(bind).get_table_names())
    if "permissions" not in tables:
        return
    keys = [key for granular in COARSE_TO_GRANULAR.values() for key, _ in granular]
    for key in keys:
        pid = _permission_id(bind, key)
        if pid:
            for table, _holder_column, _granted_by_column in _GRANT_TABLES:
                if table not in tables:
                    continue
                bind.execute(
                    sa.text(f"DELETE FROM {table} WHERE permission_id = :p"),
                    {"p": pid},
                )
    for key in keys:
        bind.execute(sa.text("DELETE FROM permissions WHERE key = :key"), {"key": key})
