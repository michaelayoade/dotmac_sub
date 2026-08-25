"""Seed granular customer-experience handoff permissions.

Revision ID: 555_cx_handoff_permissions
Revises: 554_ai_intake_canary_library
Create Date: 2026-08-25
"""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import uuid4

import sqlalchemy as sa

from alembic import op

revision: str = "555_cx_handoff_permissions"
down_revision: str | None = "554_ai_intake_canary_library"
branch_labels = None
depends_on = None

PERMISSIONS = {
    "customer_experience:handoff:read": "View customer-experience handoff queue",
    "customer_experience:handoff:accept": "Accept ready customer-experience handoffs",
    "customer_experience:handoff:attention": (
        "Flag and resolve customer-experience handoff attention items"
    ),
}


def _has_column(table: str, column: str) -> bool:
    bind = op.get_bind()
    return column in {col["name"] for col in sa.inspect(bind).get_columns(table)}


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    if "permissions" not in inspector.get_table_names():
        return
    now = datetime.now(UTC)
    has_ui_assignable = _has_column("permissions", "is_ui_assignable")
    for key, description in PERMISSIONS.items():
        existing = bind.execute(
            sa.text("SELECT id FROM permissions WHERE key = :key"),
            {"key": key},
        ).scalar()
        if existing:
            fields = [
                "description = COALESCE(description, :description)",
                "is_active = true",
                "updated_at = :now",
            ]
            if has_ui_assignable:
                fields.append("is_ui_assignable = true")
            bind.execute(
                sa.text(
                    f"""
                    UPDATE permissions
                       SET {", ".join(fields)}
                     WHERE key = :key
                    """
                ),
                {"key": key, "description": description, "now": now},
            )
            continue
        columns = ["id", "key", "description", "is_active", "created_at", "updated_at"]
        values = [":id", ":key", ":description", "true", ":now", ":now"]
        if has_ui_assignable:
            columns.insert(4, "is_ui_assignable")
            values.insert(4, "true")
        bind.execute(
            sa.text(
                f"""
                INSERT INTO permissions ({", ".join(columns)})
                VALUES ({", ".join(values)})
                """
            ),
            {
                "id": str(uuid4()),
                "key": key,
                "description": description,
                "now": now,
            },
        )


def downgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    if "permissions" not in inspector.get_table_names():
        return
    permissions = sa.table("permissions", sa.column("key"))
    bind.execute(sa.delete(permissions).where(permissions.c.key.in_(set(PERMISSIONS))))
