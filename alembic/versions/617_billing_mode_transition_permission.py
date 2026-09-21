"""Register the reviewed billing-mode transition permission.

The permission is deliberately not granted to any role by this migration.
Operators must assign it explicitly after reviewing the bidirectional billing
mode workflow.

Revision ID: 617_billing_mode_transition_permission
Revises: 616_fiber_acquisition_attribution
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import UTC, datetime
from uuid import uuid4

import sqlalchemy as sa

from alembic import op

revision: str = "617_billing_mode_transition_permission"
down_revision: str | None = "616_fiber_acquisition_attribution"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

PERMISSION_KEY = "billing:mode:write"
PERMISSION_DESCRIPTION = (
    "Preview and confirm reviewed account-wide prepaid/postpaid billing-mode changes"
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
                    is_ui_assignable = true, updated_at = :now
                WHERE key = :key
                """
            ),
            {
                "key": PERMISSION_KEY,
                "description": PERMISSION_DESCRIPTION,
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


class DowngradeRefused(RuntimeError):
    """Raised rather than deleting an operator-assigned permission grant."""


def _grant_count(bind, table_names: set[str], table: str) -> int:
    if table not in table_names:
        return 0
    return int(
        bind.execute(
            sa.text(
                f"""
                SELECT count(*) FROM {table} grant_row
                JOIN permissions permission
                  ON grant_row.permission_id = permission.id
                WHERE permission.key = :key
                """
            ),
            {"key": PERMISSION_KEY},
        ).scalar()
        or 0
    )


def downgrade() -> None:
    bind = op.get_bind()
    table_names = set(sa.inspect(bind).get_table_names())
    if "permissions" not in table_names:
        return
    grant_tables = (
        "system_user_permissions",
        "subscriber_permissions",
        "role_permissions",
    )
    if bind.dialect.name == "postgresql":
        op.execute("SET LOCAL lock_timeout = '5s'")
        op.execute("SET LOCAL statement_timeout = '15min'")
        for table in grant_tables:
            if table in table_names:
                op.execute(f"LOCK TABLE {table} IN ACCESS EXCLUSIVE MODE")
    grants = sum(_grant_count(bind, table_names, table) for table in grant_tables)
    if grants:
        raise DowngradeRefused(
            f"{grants} grant(s) of {PERMISSION_KEY!r} exist; remove them before downgrade"
        )
    bind.execute(
        sa.text("DELETE FROM permissions WHERE key = :key"),
        {"key": PERMISSION_KEY},
    )
