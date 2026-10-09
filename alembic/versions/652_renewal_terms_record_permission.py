"""seed billing:renewal_terms:record for admin and finance_manager

The finance-reviewed prepaid renewal-term record
(``app.services.prepaid_renewal_terms_backfill.request_reviewed_renewal_term_record``
and ``approve_reviewed_renewal_term_record``) writes a subscription's
contracted renewal amount after a four-eyes review: one staff member
requests, a different one approves, and BOTH must hold this permission. It is
a brand-new, narrowly scoped capability, so it is seeded here for the first
time (same shape as ``597_prepaid_draft_repair_permission``) rather than
riding on a broad billing write grant.

It is granted to ``admin`` and ``finance_manager`` — the role the RBAC seed
describes as "Full billing and finance access" — because finance staff own
these work items. Two distinct finance_manager users satisfy four-eyes.

Data-only and idempotent: re-running inserts nothing that already exists.
Downgrade removes the permission and its role grants.

Revision ID: 652_renewal_terms_record_permission
Revises: 651_prepaid_sweep_cycle_totals
Create Date: 2026-10-08
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import UTC, datetime
from uuid import uuid4

import sqlalchemy as sa

from alembic import op

revision: str = "652_renewal_terms_record_permission"
down_revision: str | None = "651_prepaid_sweep_cycle_totals"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_PERMISSION_KEY = "billing:renewal_terms:record"
_PERMISSION_DESCRIPTION = (
    "Request or approve a finance-reviewed prepaid renewal-term record "
    "(four-eyes; requester and approver must differ)"
)
_GRANTED_ROLES = ("admin", "finance_manager")


def _seed_permission() -> None:
    bind = op.get_bind()
    if not {"permissions", "roles", "role_permissions"}.issubset(
        sa.inspect(bind).get_table_names()
    ):
        return
    metadata = sa.MetaData()
    permissions = sa.Table("permissions", metadata, autoload_with=bind)
    roles = sa.Table("roles", metadata, autoload_with=bind)
    role_permissions = sa.Table("role_permissions", metadata, autoload_with=bind)
    now = datetime.now(UTC)

    permission_id = bind.execute(
        sa.select(permissions.c.id).where(permissions.c.key == _PERMISSION_KEY)
    ).scalar_one_or_none()
    if permission_id is None:
        permission_id = uuid4()
        bind.execute(
            permissions.insert().values(
                id=permission_id,
                key=_PERMISSION_KEY,
                description=_PERMISSION_DESCRIPTION,
                is_active=True,
                is_ui_assignable=True,
                created_at=now,
                updated_at=now,
            )
        )

    for role_name in _GRANTED_ROLES:
        role_id = bind.execute(
            sa.select(roles.c.id).where(
                roles.c.name == role_name,
                roles.c.is_active.is_(True),
            )
        ).scalar_one_or_none()
        if role_id is None:
            continue
        existing = bind.execute(
            sa.select(role_permissions.c.id).where(
                role_permissions.c.role_id == role_id,
                role_permissions.c.permission_id == permission_id,
            )
        ).scalar_one_or_none()
        if existing is None:
            bind.execute(
                role_permissions.insert().values(
                    id=uuid4(),
                    role_id=role_id,
                    permission_id=permission_id,
                )
            )


def _unseed_permission() -> None:
    bind = op.get_bind()
    tables = set(sa.inspect(bind).get_table_names())
    if "permissions" not in tables:
        return
    permission_id = bind.execute(
        sa.text("SELECT id FROM permissions WHERE key = :key"),
        {"key": _PERMISSION_KEY},
    ).scalar()
    if permission_id is None:
        return
    if "role_permissions" in tables:
        bind.execute(
            sa.text("DELETE FROM role_permissions WHERE permission_id = :id"),
            {"id": permission_id},
        )
    bind.execute(
        sa.text("DELETE FROM permissions WHERE id = :id"),
        {"id": permission_id},
    )


def upgrade() -> None:
    _seed_permission()


def downgrade() -> None:
    _unseed_permission()
