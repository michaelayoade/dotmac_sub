"""Add the assignable forward subscription service-invoice permission.

Revision ID: 689_forward_service_invoice_permission
Revises: 688_billing_accounting_fact_export

The permission is deliberately created without grants. Operators assign it to
the roles that need this controlled money-and-service action. Downgrade refuses
to remove a permission while any principal or role still holds it.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import UTC, datetime
from uuid import uuid4

import sqlalchemy as sa

from alembic import op

revision: str = "689_forward_service_invoice_permission"
down_revision: str | None = "688_billing_accounting_fact_export"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

PERMISSION_KEY = "billing:invoice:create_subscription_service"
PERMISSION_DESCRIPTION = (
    "Issue a reviewed invoice for a subscription's exact next service period"
)
_GRANT_TABLES = (
    "role_permissions",
    "subscriber_permissions",
    "system_user_permissions",
)


def _permission_id(bind):
    return bind.execute(
        sa.text("SELECT id FROM permissions WHERE key = :key"),
        {"key": PERMISSION_KEY},
    ).scalar()


def upgrade() -> None:
    bind = op.get_bind()
    if "permissions" not in set(sa.inspect(bind).get_table_names()):
        return
    if _permission_id(bind) is not None:
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
    tables = set(sa.inspect(bind).get_table_names())
    if "permissions" not in tables:
        return
    permission_id = _permission_id(bind)
    if permission_id is None:
        return
    holders = 0
    for table in _GRANT_TABLES:
        if table in tables:
            holders += int(
                bind.execute(
                    sa.text(f"SELECT count(*) FROM {table} WHERE permission_id = :id"),
                    {"id": permission_id},
                ).scalar()
                or 0
            )
    if holders:
        raise RuntimeError(
            f"Refusing to remove {PERMISSION_KEY}: {holders} active grants remain"
        )
    bind.execute(
        sa.text("DELETE FROM permissions WHERE id = :id"),
        {"id": permission_id},
    )
