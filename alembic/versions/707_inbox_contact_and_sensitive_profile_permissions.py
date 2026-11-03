"""Add granular Inbox contact and sensitive Customer permissions.

Revision ID: 707_inbox_contact_sensitive_permissions
Revises: 706_support_csat_report_depth
Create Date: 2026-11-03
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import UTC, datetime
from uuid import uuid4

import sqlalchemy as sa

from alembic import op

revision: str = "707_inbox_contact_sensitive_permissions"
down_revision: str | None = "706_support_csat_report_depth"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_PERMISSIONS = (
    (
        "support:inbox:contact_link",
        "Create or replace a reusable Inbox contact-to-Customer link",
        "support:ticket:update",
    ),
    (
        "customer:sensitive_profile:write",
        "Change sensitive Customer identity fields such as DOB and NIN",
        "customer:write",
    ),
)


def _permission_id(bind, key: str):
    return bind.execute(
        sa.text("SELECT id FROM permissions WHERE key = :key"), {"key": key}
    ).scalar_one_or_none()


def upgrade() -> None:
    bind = op.get_bind()
    tables = set(sa.inspect(bind).get_table_names())
    if "permissions" not in tables:
        return
    now = datetime.now(UTC)
    for key, description, source_key in _PERMISSIONS:
        permission_id = _permission_id(bind, key)
        if permission_id is None:
            permission_id = uuid4()
            bind.execute(
                sa.text(
                    "INSERT INTO permissions "
                    "(id, key, description, is_active, is_ui_assignable, created_at, updated_at) "
                    "VALUES (:id, :key, :description, true, true, :now, :now)"
                ),
                {
                    "id": permission_id,
                    "key": key,
                    "description": description,
                    "now": now,
                },
            )
        source_id = _permission_id(bind, source_key)
        if source_id is None:
            continue
        for table_name, principal_column, grant_columns in (
            ("role_permissions", "role_id", None),
            (
                "system_user_permissions",
                "system_user_id",
                ("granted_at", "granted_by_system_user_id"),
            ),
        ):
            if table_name not in tables:
                continue
            extra_select = (
                f", {grant_columns[0]}, {grant_columns[1]}" if grant_columns else ""
            )
            rows = bind.execute(
                sa.text(
                    f"SELECT {principal_column}{extra_select} FROM {table_name} "
                    "WHERE permission_id = :source_id"
                ),
                {"source_id": source_id},
            ).fetchall()
            for row in rows:
                principal_id = row[0]
                exists = bind.execute(
                    sa.text(
                        f"SELECT 1 FROM {table_name} WHERE {principal_column} = :principal_id "
                        "AND permission_id = :permission_id"
                    ),
                    {"principal_id": principal_id, "permission_id": permission_id},
                ).scalar_one_or_none()
                if not exists:
                    extra_columns = (
                        f", {grant_columns[0]}, {grant_columns[1]}"
                        if grant_columns
                        else ""
                    )
                    extra_values = ", :granted_at, :granted_by" if grant_columns else ""
                    values = {
                        "id": uuid4(),
                        "principal_id": principal_id,
                        "permission_id": permission_id,
                    }
                    if grant_columns:
                        values.update(
                            {
                                "granted_at": row[1] or now,
                                "granted_by": row[2],
                            }
                        )
                    bind.execute(
                        sa.text(
                            f"INSERT INTO {table_name} "
                            f"(id, {principal_column}, permission_id{extra_columns}) "
                            f"VALUES (:id, :principal_id, :permission_id{extra_values})"
                        ),
                        values,
                    )


def downgrade() -> None:
    raise RuntimeError(
        "Remove grants for the granular Inbox permissions before downgrading."
    )
