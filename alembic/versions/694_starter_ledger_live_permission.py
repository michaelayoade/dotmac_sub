"""Provision the dedicated Starter ledger-live switch permission.

D1 hold: only users explicitly granted ``billing:starter_ledger_live:write``
may change ``billing.starter_ledger_live_enabled``. The permission is created
here and granted to NO role or user; the owner grants it to himself and a named
backup approver.

Revision ID: 694_starter_ledger_live_permission
Revises: 693_starter_credit_note_links
Create Date: 2026-10-19
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import UTC, datetime
from uuid import uuid4

import sqlalchemy as sa

from alembic import op

revision: str = "694_starter_ledger_live_permission"
down_revision: str | None = "693_starter_credit_note_links"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

PERMISSION_KEY = "billing:starter_ledger_live:write"
PERMISSION_DESCRIPTION = (
    "Change the Starter ledger live (D1) switch; granted to no role by default"
)


def upgrade() -> None:
    bind = op.get_bind()
    if "permissions" not in sa.inspect(bind).get_table_names():
        return
    exists = bind.execute(
        sa.text("SELECT 1 FROM permissions WHERE key = :key"), {"key": PERMISSION_KEY}
    ).scalar()
    if exists:
        return
    now = datetime.now(UTC)
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
            "key": PERMISSION_KEY,
            "description": PERMISSION_DESCRIPTION,
            "now": now,
        },
    )


def downgrade() -> None:
    bind = op.get_bind()
    tables = sa.inspect(bind).get_table_names()
    if "permissions" not in tables:
        return
    row = bind.execute(
        sa.text("SELECT id FROM permissions WHERE key = :key"), {"key": PERMISSION_KEY}
    ).scalar()
    if row is None:
        return
    for table, column in (
        ("role_permissions", "permission_id"),
        ("system_user_permissions", "permission_id"),
    ):
        if table in tables:
            bind.execute(
                sa.text(f"DELETE FROM {table} WHERE {column} = :id"), {"id": row}
            )
    bind.execute(sa.text("DELETE FROM permissions WHERE id = :id"), {"id": row})
