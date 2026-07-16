"""Add reseller:read/reseller:write and grant them to current customer-perm holders.

The reseller admin surface is split off the shared customer:read/customer:write
permissions onto dedicated reseller:read/reseller:write. To preserve exactly who
can view/manage resellers today, this migration grants reseller:read to every role
that holds customer:read and reseller:write to every role that holds customer:write.
customer:read/write are unchanged (still used for customer management). Admins can
tighten reseller access afterwards via the role builder.

Revision ID: 315_reseller_granular_permissions
Revises: 314_retire_coarse_dispatch_permission
"""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import uuid4

import sqlalchemy as sa

from alembic import op

revision = "315_reseller_granular_permissions"
down_revision = "314_retire_coarse_dispatch_permission"
branch_labels = None
depends_on = None

# (new granular permission, description, source permission whose holders inherit it)
GRANTS = [
    ("reseller:read", "View resellers", "customer:read"),
    ("reseller:write", "Manage resellers", "customer:write"),
]


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

    for key, description, source_key in GRANTS:
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
        if "role_permissions" not in tables:
            continue
        source_id = _permission_id(bind, source_key)
        if not source_id:
            continue
        role_ids = [
            row[0]
            for row in bind.execute(
                sa.text(
                    "SELECT role_id FROM role_permissions WHERE permission_id = :p"
                ),
                {"p": source_id},
            ).fetchall()
        ]
        for role_id in role_ids:
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
                        "INSERT INTO role_permissions (id, role_id, permission_id) "
                        "VALUES (:id, :r, :p)"
                    ),
                    {"id": str(uuid4()), "r": role_id, "p": pid},
                )


def downgrade() -> None:
    bind = op.get_bind()
    tables = sa.inspect(bind).get_table_names()
    if "permissions" not in tables:
        return
    for key, _description, _source in GRANTS:
        pid = _permission_id(bind, key)
        if not pid:
            continue
        if "role_permissions" in tables:
            bind.execute(
                sa.text("DELETE FROM role_permissions WHERE permission_id = :p"),
                {"p": pid},
            )
        bind.execute(sa.text("DELETE FROM permissions WHERE id = :p"), {"p": pid})
