"""Add reports:support:read and grant it to the roles that should hold it.

The support/inbox reports (technician performance, ticket SLA, inbox
performance and escalations) were gated on the unrelated ``provisioning:read``.
They now require ``reports:support:read``. This seeds the permission and grants
it to every role that holds ``provisioning:read`` (so no principal loses the
access it has today) or ``support:ticket:read`` (so the support role, the
intended consumer, gains access to its own reports). ``provisioning:read`` is
left in place — it still gates real provisioning data.

Revision ID: 362_reports_support_permission
Revises: 361_retire_coarse_gis_edit_permission
"""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import uuid4

import sqlalchemy as sa

from alembic import op

revision = "362_reports_support_permission"
down_revision = "361_retire_coarse_gis_edit_permission"
branch_labels = None
depends_on = None

NEW_KEY = "reports:support:read"
NEW_DESCRIPTION = "View support and inbox operations reports"
SOURCE_KEYS = ("provisioning:read", "support:ticket:read")


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

    new_id = _permission_id(bind, NEW_KEY)
    if not new_id:
        new_id = str(uuid4())
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
            {"id": new_id, "key": NEW_KEY, "description": NEW_DESCRIPTION, "now": now},
        )

    if "role_permissions" not in tables:
        return
    source_ids = [pid for key in SOURCE_KEYS if (pid := _permission_id(bind, key))]
    if not source_ids:
        return
    role_ids = {
        row[0]
        for row in bind.execute(
            sa.text(
                "SELECT DISTINCT role_id FROM role_permissions "
                "WHERE permission_id IN :ids"
            ).bindparams(sa.bindparam("ids", expanding=True)),
            {"ids": source_ids},
        ).fetchall()
    }
    for role_id in role_ids:
        already = bind.execute(
            sa.text(
                "SELECT 1 FROM role_permissions "
                "WHERE role_id = :r AND permission_id = :p"
            ),
            {"r": role_id, "p": new_id},
        ).scalar()
        if not already:
            bind.execute(
                sa.text(
                    "INSERT INTO role_permissions (id, role_id, permission_id) "
                    "VALUES (:id, :r, :p)"
                ),
                {"id": str(uuid4()), "r": role_id, "p": new_id},
            )


def downgrade() -> None:
    bind = op.get_bind()
    tables = sa.inspect(bind).get_table_names()
    if "permissions" not in tables:
        return
    pid = _permission_id(bind, NEW_KEY)
    if not pid:
        return
    if "role_permissions" in tables:
        bind.execute(
            sa.text("DELETE FROM role_permissions WHERE permission_id = :p"),
            {"p": pid},
        )
    bind.execute(sa.text("DELETE FROM permissions WHERE key = :key"), {"key": NEW_KEY})
