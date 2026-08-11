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

So this migration retires 507's two deviations and the Sub-only scope CHECK
added by 514. All three changes make Sub's table match
`dotmac_kernel.settings_models.DomainSetting` exactly, which is what the
settings cutover needs; the upgrade changes no rows.

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

**3. The scope CHECK.** Migration 514 added a Sub-only CHECK tying `scope_kind`
to `tenant_id`. The kernel has no such constraint — it derives the value at the
write boundary instead — so retaining it would preserve a third schema
deviation and make the kernel's tenant server default reject raw inserts.

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
SCOPE_CONSTRAINT = "ck_domain_settings_scope_alignment"
KERNEL_SCOPE_DEFAULT = "tenant"
SUB_507_SCOPE_DEFAULT = "platform"
SUB_SCOPE_ALIGNMENT = (
    "(scope_kind = 'platform' AND tenant_id IS NULL) "
    "OR (scope_kind <> 'platform' AND tenant_id IS NOT NULL)"
)


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
    # 514's Sub-only CHECK is not part of the kernel table shape. Keeping it
    # while adopting the kernel's tenant server default makes a raw insert
    # produce tenant + NULL and fail immediately.
    op.execute(
        sa.text(
            f"ALTER TABLE {TABLE} DROP CONSTRAINT IF EXISTS {SCOPE_CONSTRAINT}"
        ),
    )

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

    # Rows written through the kernel-shaped schema may have relied on its
    # tenant default without supplying a tenant ID. Convert only that shape
    # before restoring 514's stricter Sub invariant.
    op.get_bind().execute(
        sa.text(
            f"UPDATE {TABLE} SET scope_kind = 'platform' "
            "WHERE scope_kind = 'tenant' AND tenant_id IS NULL"
        ),
    )
    op.create_check_constraint(SCOPE_CONSTRAINT, TABLE, SUB_SCOPE_ALIGNMENT)
