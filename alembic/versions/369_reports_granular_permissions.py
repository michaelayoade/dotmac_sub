"""Add granular reports permissions and grant them to coarse-permission holders.

The admin reports surface is split from the coarse ``reports:billing`` /
``reports:network`` (which each gated both viewing and CSV export) into
``:read`` / ``:export``. This migration seeds the granular permissions on
existing databases and grants both to every role that already holds the matching
coarse permission, so no principal loses access. Migration 370 then retires the
coarse keys once the routes declare the granular ones.

Revision ID: 369_reports_granular_permissions
Revises: 368_merge_legacy_ip_assignments_branch
"""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import uuid4

import sqlalchemy as sa

from alembic import op

revision = "369_reports_granular_permissions"
down_revision = "368_merge_legacy_ip_assignments_branch"
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


def _permission_id(bind, key: str):
    return bind.execute(
        sa.text("SELECT id FROM permissions WHERE key = :key"), {"key": key}
    ).scalar()


def upgrade() -> None:
    bind = op.get_bind()
    tables = sa.inspect(bind).get_table_names()
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

    if "role_permissions" not in tables:
        return
    for coarse_key, granular in COARSE_TO_GRANULAR.items():
        coarse_id = _permission_id(bind, coarse_key)
        if not coarse_id:
            continue
        role_ids = [
            row[0]
            for row in bind.execute(
                sa.text(
                    "SELECT role_id FROM role_permissions WHERE permission_id = :p"
                ),
                {"p": coarse_id},
            ).fetchall()
        ]
        for role_id in role_ids:
            for key, _ in granular:
                pid = granular_ids[key]
                already = bind.execute(
                    sa.text(
                        "SELECT 1 FROM role_permissions "
                        "WHERE role_id = :r AND permission_id = :p"
                    ),
                    {"r": role_id, "p": pid},
                ).scalar()
                if not already:
                    bind.execute(
                        sa.text(
                            "INSERT INTO role_permissions "
                            "(id, role_id, permission_id) VALUES (:id, :r, :p)"
                        ),
                        {"id": str(uuid4()), "r": role_id, "p": pid},
                    )


def downgrade() -> None:
    bind = op.get_bind()
    tables = sa.inspect(bind).get_table_names()
    if "permissions" not in tables:
        return
    keys = [key for granular in COARSE_TO_GRANULAR.values() for key, _ in granular]
    if "role_permissions" in tables:
        for key in keys:
            pid = _permission_id(bind, key)
            if pid:
                bind.execute(
                    sa.text("DELETE FROM role_permissions WHERE permission_id = :p"),
                    {"p": pid},
                )
    for key in keys:
        bind.execute(sa.text("DELETE FROM permissions WHERE key = :key"), {"key": key})
