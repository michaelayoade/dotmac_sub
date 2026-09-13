"""Register catalog:offer_version:admission. Not seeded into any role.

Idempotent: reruns update the existing row rather than duplicating it. This
mirrors 608_offer_access_requirement_classify_permission's exact pattern:
this migration seeds the permission row only.

Admission authorization is decided entirely at the route layer
(``app/api/catalog.py``'s ``require_any_permission(catalog:billing_write,
catalog:offer_version:admission)`` dependency) -- a caller holding the
existing ``catalog:billing_write`` continues to admit an offer version
exactly as before, and this permission is a genuine, narrower, OPT-IN
future delegation path, not a hard requirement layered on top of existing
access. There is therefore nothing to copy from ``catalog:billing_write``'s
existing grants: no role loses or gains admission ability as a side effect
of this migration, so (unlike an earlier version of this migration) there is
no grant-copying logic here, and no risk of an incomplete copy regressing an
API-key or direct-permission-grant principal.

Revision ID: 609_offer_version_admission_permission
Revises: 608_offer_access_requirement_classify_permission
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import UTC, datetime
from uuid import uuid4

import sqlalchemy as sa

from alembic import op

revision: str = "609_offer_version_admission_permission"
down_revision: str | None = "608_offer_access_requirement_classify_permission"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

PERMISSION_KEY = "catalog:offer_version:admission"
PERMISSION_DESCRIPTION = (
    "Admit or update an offer version via "
    "service_intent.offer_access_requirement (an alternative to "
    "catalog:billing_write, not a replacement for it)"
)


def upgrade() -> None:
    bind = op.get_bind()
    table_names = set(sa.inspect(bind).get_table_names())
    if "permissions" not in table_names:
        return
    now = datetime.now(UTC)
    permission_id = bind.execute(
        sa.text("SELECT id FROM permissions WHERE key = :key"),
        {"key": PERMISSION_KEY},
    ).scalar()
    if permission_id:
        bind.execute(
            sa.text(
                """
                UPDATE permissions
                SET description = :description, is_active = true,
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
                ) VALUES (:id, :key, :description, true, true, :now, :now)
                """
            ),
            {
                "id": str(uuid4()),
                "key": PERMISSION_KEY,
                "description": PERMISSION_DESCRIPTION,
                "now": now,
            },
        )
    # Deliberately no role_permissions insert: this permission is not seeded
    # into any role by default.


def downgrade() -> None:
    bind = op.get_bind()
    table_names = set(sa.inspect(bind).get_table_names())
    if "permissions" not in table_names:
        return
    if "role_permissions" in table_names:
        bind.execute(
            sa.text(
                """
                DELETE FROM role_permissions rp
                USING permissions p
                WHERE rp.permission_id = p.id AND p.key = :key
                """
            ),
            {"key": PERMISSION_KEY},
        )
    bind.execute(
        sa.text("DELETE FROM permissions WHERE key = :key"), {"key": PERMISSION_KEY}
    )
