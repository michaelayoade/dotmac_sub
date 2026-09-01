"""Seed assignable Workqueue audience permissions.

Revision ID: 571_seed_workqueue_audience_permissions
Revises: 570_ai_intake_customer_response_timeout
Create Date: 2026-09-01

Workqueue audience resolution already consumes these explicit RBAC keys, but
they were never added to the permission catalog. Non-admin staff therefore
could not receive team- or organization-wide audience through the role builder.

This migration only makes the permissions available for assignment. It does
not grant either audience to an existing role or principal.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import UTC, datetime
from uuid import uuid4

import sqlalchemy as sa

from alembic import op

revision: str = "571_seed_workqueue_audience_permissions"
down_revision: str | None = "570_ai_intake_customer_response_timeout"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

PERMISSIONS: tuple[tuple[str, str], ...] = (
    (
        "workqueue:audience:team",
        "View Workqueue items for teams where the user is a queue lead or "
        "accountable manager",
    ),
    (
        "workqueue:audience:org",
        "View all Workqueue items across the organization regardless of team, "
        "region, or assignment",
    ),
)

GRANT_TABLES: tuple[str, ...] = (
    "role_permissions",
    "subscriber_permissions",
    "system_user_permissions",
)


def _permission_id(bind, key: str) -> str | None:
    return bind.execute(
        sa.text("SELECT id FROM permissions WHERE key = :key"),
        {"key": key},
    ).scalar()


def _ensure_permission(bind, *, key: str, description: str, now: datetime) -> None:
    permission_id = _permission_id(bind, key)
    if permission_id is not None:
        bind.execute(
            sa.text(
                """
                UPDATE permissions
                SET description = :description,
                    is_active = true,
                    is_ui_assignable = true,
                    updated_at = :now
                WHERE id = :id
                """
            ),
            {
                "id": permission_id,
                "description": description,
                "now": now,
            },
        )
        return

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
            "id": str(uuid4()),
            "key": key,
            "description": description,
            "now": now,
        },
    )


def upgrade() -> None:
    bind = op.get_bind()
    tables = set(sa.inspect(bind).get_table_names())
    if "permissions" not in tables:
        return

    now = datetime.now(UTC)
    for key, description in PERMISSIONS:
        _ensure_permission(bind, key=key, description=description, now=now)


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
                grant_table = sa.table(table, sa.column("permission_id"))
                bind.execute(
                    sa.delete(grant_table).where(
                        grant_table.c.permission_id == permission_id
                    )
                )
        bind.execute(
            sa.text("DELETE FROM permissions WHERE id = :id"),
            {"id": permission_id},
        )
