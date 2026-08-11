"""domain_settings converges on the kernel tenant FK and scope default

Migration 507 gave `domain_settings` the kernel's scope columns and deliberately
deviated on two points, for a reason it stated plainly: Sub had no tenant, so a
row could not honestly claim `tenant` scope, and there was nothing for a foreign
key to point at. It backfilled `'platform'` and set that as the server default.

That reason has expired. Migration 508 created `tenants` and the operator tenant
is provisioned at boot (`app/services/operator_tenant.py`). Measured on
production 2026-08-11: **577 of 577 rows are `scope_kind='tenant'`**, all
pointing at the single operator tenant, and **zero** rows are platform-scoped or
carry a NULL `tenant_id`. The stamping in `app/models/domain_settings.py` has
been doing this on every write.

So this migration retires 507's two deviations and nothing else. Both changes
make Sub's table match `dotmac_kernel.settings_models.DomainSetting` exactly,
which is what the settings cutover needs; neither changes a single row.

**1. The server default.** Sub's is `'platform'`; the kernel's is `'tenant'`.
The kernel also derives the value in Python when the caller does not say
(`_default_scope_kind`: platform when `tenant_id` is NULL, tenant otherwise), so
the server default only decides what a write that bypasses the ORM gets. Today
that write lands at `platform` scope with a NULL `tenant_id` — the exact
incoherent row 507 refused to create, now reachable precisely BECAUSE a tenant
exists. After this it lands as `tenant`, matching every row already there.

**2. The foreign key.** The kernel has `tenant_id -> tenants.id ON DELETE
CASCADE`; Sub has none, so nothing at the database level ties those 577 rows to
the tenant they name. The column stays NULLABLE on purpose — in the kernel's
model `tenant_id IS NULL` *is* the platform scope, so making it NOT NULL would
diverge from the shape this migration exists to converge on.

Deliberately NOT added: a CHECK tying `scope_kind` to `tenant_id`. The kernel has
no such constraint — it derives the value at the write boundary instead — and
inventing one here would be a Sub-only deviation reintroduced in the same change
that removes two others.

Revision ID: 518_domain_settings_converge_on_kernel_shape
Revises: 517_close_legacy_resolved_tickets
Create Date: 2026-08-11
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "518_domain_settings_converge_on_kernel_shape"
down_revision: str | None = "517_close_legacy_resolved_tickets"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

TABLE = "domain_settings"
FK_NAME = "fk_domain_settings_tenant"
KERNEL_SCOPE_DEFAULT = "tenant"
SUB_507_SCOPE_DEFAULT = "platform"


def _fk_exists(name: str) -> bool:
    return bool(
        op.get_bind()
        .execute(
            sa.text(
                "SELECT 1 FROM information_schema.table_constraints "
                "WHERE constraint_name = :n AND constraint_type = 'FOREIGN KEY'"
            ),
            {"n": name},
        )
        .scalar()
    )


def upgrade() -> None:
    op.alter_column(
        TABLE,
        "scope_kind",
        server_default=sa.text(f"'{KERNEL_SCOPE_DEFAULT}'"),
        existing_type=sa.String(length=40),
        existing_nullable=False,
    )

    # Guarded because `tenants` is created by 508 behind its own `_has_table`
    # check, so a database restored from before that point must not fail here.
    if not _fk_exists(FK_NAME):
        op.create_foreign_key(
            FK_NAME,
            TABLE,
            "tenants",
            ["tenant_id"],
            ["id"],
            ondelete="CASCADE",
        )


def downgrade() -> None:
    if _fk_exists(FK_NAME):
        op.drop_constraint(FK_NAME, TABLE, type_="foreignkey")

    op.alter_column(
        TABLE,
        "scope_kind",
        server_default=sa.text(f"'{SUB_507_SCOPE_DEFAULT}'"),
        existing_type=sa.String(length=40),
        existing_nullable=False,
    )
