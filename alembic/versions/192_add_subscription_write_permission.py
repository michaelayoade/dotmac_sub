"""Add UI-assignable subscription write permission.

Revision ID: 192_add_subscription_write_permission
Revises: 191_add_quote_mirror
"""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import uuid4

import sqlalchemy as sa

from alembic import op

revision = "192_add_subscription_write_permission"
down_revision = "191_add_quote_mirror"
branch_labels = None
depends_on = None

PERMISSION_KEY = "subscription:write"
PERMISSION_DESCRIPTION = "Manage subscriptions"


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    if "permissions" not in inspector.get_table_names():
        return

    now = datetime.now(UTC)
    existing = bind.execute(
        sa.text("SELECT id FROM permissions WHERE key = :key"),
        {"key": PERMISSION_KEY},
    ).scalar()
    if existing:
        bind.execute(
            sa.text(
                """
                UPDATE permissions
                SET description = COALESCE(description, :description),
                    is_active = true,
                    is_ui_assignable = true,
                    updated_at = :now
                WHERE key = :key
                """
            ),
            {"key": PERMISSION_KEY, "description": PERMISSION_DESCRIPTION, "now": now},
        )
    else:
        bind.execute(
            sa.text(
                """
                INSERT INTO permissions (
                    id, key, description, is_active, is_ui_assignable,
                    created_at, updated_at
                )
                VALUES (
                    :id, :key, :description, true, true, :now, :now
                )
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
    inspector = sa.inspect(bind)
    if "permissions" not in inspector.get_table_names():
        return

    bind.execute(
        sa.text("DELETE FROM permissions WHERE key = :key"),
        {"key": PERMISSION_KEY},
    )
