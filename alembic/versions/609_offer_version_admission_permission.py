"""seed catalog:offer_version:admission and grant it to every catalog:billing_write role.

``app.services.catalog.offer_access_requirement._verify_admission_permission``
gates ``admit_offer_version`` (the sole writer behind ``POST /offer-versions``
and ``PATCH`` today, since the route only ever supplies a real
``system_user`` actor or none) on ``catalog:offer_version:admission``. That
permission previously had no seed migration at all: nothing granted it to any
role, so every existing staff/API-key principal who could create an offer
version yesterday (by holding ``catalog:billing_write``, checked by
``app/api/catalog.py``'s ``_require_billing_catalog_write`` dependency) would
get a 403 today. This is a production-breaking regression, not a
"new capability, opt in later" case like ``597_prepaid_draft_repair_permission``
-- it must preserve exactly the set of principals who could already admit an
offer version.

The fix derives the grant set from the live data instead of a guessed role
name: every role that currently holds ``catalog:billing_write`` receives
``catalog:offer_version:admission`` too. A role granted ``catalog:billing_write``
after this migration runs does not automatically receive this permission --
that is an acceptable, separately reviewable follow-up grant, not a
regression this migration is responsible for closing.

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

_ADMISSION_PERMISSION_KEY = "catalog:offer_version:admission"
_ADMISSION_PERMISSION_DESCRIPTION = (
    "Admit a new offer version via service_intent.offer_access_requirement"
)
_BILLING_WRITE_PERMISSION_KEY = "catalog:billing_write"


def _required_tables(bind) -> bool:
    return {"permissions", "roles", "role_permissions"}.issubset(
        sa.inspect(bind).get_table_names()
    )


def upgrade() -> None:
    bind = op.get_bind()
    if not _required_tables(bind):
        return
    metadata = sa.MetaData()
    permissions = sa.Table("permissions", metadata, autoload_with=bind)
    role_permissions = sa.Table("role_permissions", metadata, autoload_with=bind)
    now = datetime.now(UTC)

    admission_permission_id = bind.execute(
        sa.select(permissions.c.id).where(
            permissions.c.key == _ADMISSION_PERMISSION_KEY
        )
    ).scalar_one_or_none()
    if admission_permission_id is None:
        admission_permission_id = uuid4()
        bind.execute(
            permissions.insert().values(
                id=admission_permission_id,
                key=_ADMISSION_PERMISSION_KEY,
                description=_ADMISSION_PERMISSION_DESCRIPTION,
                is_active=True,
                is_ui_assignable=True,
                created_at=now,
                updated_at=now,
            )
        )
    else:
        bind.execute(
            permissions.update()
            .where(permissions.c.id == admission_permission_id)
            .values(
                description=_ADMISSION_PERMISSION_DESCRIPTION,
                is_active=True,
                updated_at=now,
            )
        )

    billing_write_permission_id = bind.execute(
        sa.select(permissions.c.id).where(
            permissions.c.key == _BILLING_WRITE_PERMISSION_KEY
        )
    ).scalar_one_or_none()
    if billing_write_permission_id is None:
        # No billing_write permission row exists at all (e.g. a fresh test
        # database seeded past 280 without ever inserting it) -- nothing to
        # derive a grant set from.
        return

    # Derive the grant set from live data: every role that currently holds
    # catalog:billing_write, never a hardcoded role name.
    billing_write_role_ids = [
        row[0]
        for row in bind.execute(
            sa.select(role_permissions.c.role_id).where(
                role_permissions.c.permission_id == billing_write_permission_id
            )
        )
    ]
    if not billing_write_role_ids:
        return

    existing_admission_role_ids = {
        row[0]
        for row in bind.execute(
            sa.select(role_permissions.c.role_id).where(
                role_permissions.c.permission_id == admission_permission_id
            )
        )
    }
    for role_id in billing_write_role_ids:
        if role_id in existing_admission_role_ids:
            continue
        bind.execute(
            role_permissions.insert().values(
                id=uuid4(),
                role_id=role_id,
                permission_id=admission_permission_id,
            )
        )
        existing_admission_role_ids.add(role_id)


def downgrade() -> None:
    bind = op.get_bind()
    tables = set(sa.inspect(bind).get_table_names())
    if "permissions" not in tables:
        return
    permission_id = bind.execute(
        sa.text("SELECT id FROM permissions WHERE key = :key"),
        {"key": _ADMISSION_PERMISSION_KEY},
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
