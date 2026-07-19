"""Retire the coarse reports:billing / reports:network permissions.

The admin reports routes now declare ``reports:billing:read`` / ``:export`` and
``reports:network:read`` / ``:export``, so the coarse ``reports:billing`` and
``reports:network`` permissions have no remaining consumer. Migration 369 already
granted the granular permissions to every role that held a coarse one, so
deleting them here removes no access.

Revision ID: 370_retire_coarse_reports_permissions
Revises: 369_reports_granular_permissions
"""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import uuid4

import sqlalchemy as sa

from alembic import op

revision = "370_retire_coarse_reports_permissions"
down_revision = "369_reports_granular_permissions"
branch_labels = None
depends_on = None

COARSE = {
    "reports:billing": "Billing and revenue reports",
    "reports:network": "Network and bandwidth reports",
}


def upgrade() -> None:
    bind = op.get_bind()
    tables = sa.inspect(bind).get_table_names()
    if "permissions" not in tables:
        return
    for key in COARSE:
        pid = bind.execute(
            sa.text("SELECT id FROM permissions WHERE key = :key"), {"key": key}
        ).scalar()
        if not pid:
            continue
        if "role_permissions" in tables:
            bind.execute(
                sa.text("DELETE FROM role_permissions WHERE permission_id = :p"),
                {"p": pid},
            )
        bind.execute(sa.text("DELETE FROM permissions WHERE id = :p"), {"p": pid})


def downgrade() -> None:
    bind = op.get_bind()
    tables = sa.inspect(bind).get_table_names()
    if "permissions" not in tables:
        return
    now = datetime.now(UTC)
    for key, description in COARSE.items():
        existing = bind.execute(
            sa.text("SELECT id FROM permissions WHERE key = :key"), {"key": key}
        ).scalar()
        if existing:
            continue
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
            {"id": str(uuid4()), "key": key, "description": description, "now": now},
        )
