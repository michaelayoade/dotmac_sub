"""Register permission-gated backdated service invoice actions.

The permissions are UI-assignable and deliberately receive no role grants in
this migration.  Administrators can attach either capability to the exact roles
that require it; the workflow is not coupled to a Finance approver role.

Revision ID: 688_backdated_invoice_permissions
Revises: 686_field_lifecycle_router_health_merge
Create Date: 2026-10-13
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import UTC, datetime
from uuid import uuid4

import sqlalchemy as sa

from alembic import op

revision: str = "688_backdated_invoice_permissions"
down_revision: str | None = "686_field_lifecycle_router_health_merge"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_PERMISSIONS = (
    (
        "billing:invoice:create_backdated_service",
        "Create reviewed draft invoices for consumed subscription service",
    ),
    (
        "billing:invoice:reconstruct_backdated_service",
        "Link an eligible existing invoice to reviewed subscription service",
    ),
)


def upgrade() -> None:
    bind = op.get_bind()
    if "permissions" not in sa.inspect(bind).get_table_names():
        return
    metadata = sa.MetaData()
    permissions = sa.Table("permissions", metadata, autoload_with=bind)
    now = datetime.now(UTC)
    for key, description in _PERMISSIONS:
        permission_id = bind.execute(
            sa.select(permissions.c.id).where(permissions.c.key == key)
        ).scalar_one_or_none()
        if permission_id is None:
            bind.execute(
                permissions.insert().values(
                    id=uuid4(),
                    key=key,
                    description=description,
                    is_active=True,
                    is_ui_assignable=True,
                    created_at=now,
                    updated_at=now,
                )
            )
        else:
            bind.execute(
                permissions.update()
                .where(permissions.c.id == permission_id)
                .values(
                    description=description,
                    is_active=True,
                    is_ui_assignable=True,
                    updated_at=now,
                )
            )


class DowngradeRefused(RuntimeError):
    """Raised rather than deleting a permission that operators assigned."""


def downgrade() -> None:
    bind = op.get_bind()
    table_names = set(sa.inspect(bind).get_table_names())
    if "permissions" not in table_names:
        return
    keys = tuple(key for key, _description in _PERMISSIONS)
    permission_ids = tuple(
        bind.execute(
            sa.text("SELECT id FROM permissions WHERE key IN :keys").bindparams(
                sa.bindparam("keys", expanding=True)
            ),
            {"keys": keys},
        ).scalars()
    )
    if not permission_ids:
        return
    grant_tables = (
        "system_user_permissions",
        "subscriber_permissions",
        "role_permissions",
    )
    metadata = sa.MetaData()
    grants = 0
    for table in grant_tables:
        if table not in table_names:
            continue
        grant_table = sa.Table(table, metadata, autoload_with=bind)
        grants += int(
            bind.execute(
                sa.select(sa.func.count())
                .select_from(grant_table)
                .where(grant_table.c.permission_id.in_(permission_ids))
            ).scalar()
            or 0
        )
    if grants:
        raise DowngradeRefused(
            f"{grants} backdated service invoice permission grant(s) exist; "
            "remove them before downgrade"
        )
    bind.execute(
        sa.text("DELETE FROM permissions WHERE id IN :ids").bindparams(
            sa.bindparam("ids", expanding=True)
        ),
        {"ids": permission_ids},
    )
