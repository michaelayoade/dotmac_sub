"""Let a credential name the Party it authenticates, and the binding that proves it

R1 of the Party/principal adoption slice. **Additive only.** It adds columns and
one table; it enforces no uniqueness, removes no legacy column, binds no row and
touches no reader. The legacy `subscriber_id`/`system_user_id`/`reseller_user_id`
principal FKs and their exactly-one CHECK are untouched and remain authoritative.

## Why a binding table rather than a `provider` column

ADR-0019 (starter) rules that a credential authenticates a **Party**, and may
repeat only per **authentication mechanism** — never per principal kind, portal,
account or membership. The discriminator is therefore "which mechanism proved
you are this party", and it must be the installed **binding**, not the mechanism
*code*: two OIDC issuers, or two RADIUS verifiers, are two bindings of one code,
and a code-keyed constraint would forbid a party holding a credential against
each.

Measured on production 2026-08-12: Sub has exactly one binding per mechanism
today — `local` (4,273 of 4,273 credentials), one `radius_servers` row with zero
credentials pointing at it, and `AuthProvider.sso` implemented nowhere. So a
code-keyed constraint would work *today*, by accident of configuration rather
than by structure. This table costs two rows now and is expensive to retrofit
after a uniqueness constraint ships.

`mechanism_code` is a plain string on purpose (ADR-0008): the vocabulary is open
and declared, so a deployment names a mechanism without a migration. The typed
contract lives behind the binding, in the provider module — which is why nothing
here has a `radius_server_id` column.

## The evidence quartet

`party_id`/`party_bound_at`/`party_binding_source`/`party_binding_reason` follow
the shape Sub already uses on `subscribers`, `system_users`, `reseller_users`
and `subscriber_contacts`: all four present or all four absent, enforced by
CHECK, so a binding can never exist without reviewed provenance.

## What this deliberately does NOT do

- no UNIQUE on `(party_id, authentication_binding_id)` — R3's job, and it would
  force credential merging, which needs a security policy and production
  evidence that do not exist yet;
- no NOT NULL, no RLS, no legacy-column removal;
- no backfill. Production has 4,102 unbound credential-bearing principals; they
  are bound by the reviewed command, in approved capped batches, never by DDL.

Revision ID: 524_credential_party_binding_additive
Revises: 523_domain_settings_tenant_fk
Create Date: 2026-08-12
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "524_credential_party_binding_additive"
down_revision: str | None = "523_domain_settings_tenant_fk"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

BINDINGS = "authentication_bindings"
CREDENTIALS = "user_credentials"

#: Seeded from the production measurement. `sso` is deliberately absent: it is
#: declared in `AuthProvider` and implemented nowhere, and an open vocabulary
#: should carry zero declarations for a capability that does not exist.
_SEED = (
    ("local", "Local password", "Password verified against the stored hash."),
    ("radius", "RADIUS", "Password verified by the configured RADIUS target."),
)

_EVIDENCE = (
    "(party_id IS NULL AND party_bound_at IS NULL AND "
    "party_binding_source IS NULL AND party_binding_reason IS NULL) OR "
    "(party_id IS NOT NULL AND party_bound_at IS NOT NULL AND "
    "party_binding_source IS NOT NULL AND party_binding_reason IS NOT NULL AND "
    "length(trim(party_binding_source)) > 0 AND "
    "length(trim(party_binding_reason)) > 0)"
)


def upgrade() -> None:
    op.create_table(
        BINDINGS,
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("mechanism_code", sa.String(length=40), nullable=False),
        sa.Column("name", sa.String(length=120), nullable=False),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column(
            "is_active", sa.Boolean(), nullable=False, server_default=sa.text("true")
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.UniqueConstraint(
            "mechanism_code", "name", name="uq_auth_bindings_code_name"
        ),
        sa.CheckConstraint(
            "length(trim(mechanism_code)) > 0", name="ck_auth_bindings_code_nonempty"
        ),
    )
    op.create_index(
        "ix_auth_bindings_code_active", BINDINGS, ["mechanism_code", "is_active"]
    )

    bindings = sa.table(
        BINDINGS,
        sa.column("id", sa.Uuid()),
        sa.column("mechanism_code", sa.String()),
        sa.column("name", sa.String()),
        sa.column("description", sa.Text()),
    )
    op.bulk_insert(
        bindings,
        [
            {
                "id": sa.func.gen_random_uuid(),
                "mechanism_code": code,
                "name": name,
                "description": description,
            }
            for code, name, description in _SEED
        ],
    )

    op.add_column(CREDENTIALS, sa.Column("party_id", sa.Uuid(), nullable=True))
    op.add_column(
        CREDENTIALS,
        sa.Column("party_bound_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.add_column(
        CREDENTIALS,
        sa.Column("party_binding_source", sa.String(length=80), nullable=True),
    )
    op.add_column(
        CREDENTIALS, sa.Column("party_binding_reason", sa.Text(), nullable=True)
    )
    op.add_column(
        CREDENTIALS, sa.Column("authentication_binding_id", sa.Uuid(), nullable=True)
    )
    # Nullable, and it stays nullable. Sub has one operator tenant; the kernel's
    # contract wants the column, but making it NOT NULL is R3's job and depends
    # on the GUC/session contract landing first.
    op.add_column(CREDENTIALS, sa.Column("tenant_id", sa.Uuid(), nullable=True))

    op.create_foreign_key(
        "fk_user_credentials_party",
        CREDENTIALS,
        "parties",
        ["party_id"],
        ["id"],
        ondelete="RESTRICT",
    )
    op.create_foreign_key(
        "fk_user_credentials_auth_binding",
        CREDENTIALS,
        BINDINGS,
        ["authentication_binding_id"],
        ["id"],
        ondelete="RESTRICT",
    )
    op.create_foreign_key(
        "fk_user_credentials_tenant",
        CREDENTIALS,
        "tenants",
        ["tenant_id"],
        ["id"],
        ondelete="RESTRICT",
    )
    op.create_check_constraint(
        "ck_user_credentials_party_binding_evidence", CREDENTIALS, _EVIDENCE
    )
    op.create_index("ix_user_credentials_party_id", CREDENTIALS, ["party_id"])
    op.create_index(
        "ix_user_credentials_auth_binding", CREDENTIALS, ["authentication_binding_id"]
    )


def downgrade() -> None:
    op.drop_index("ix_user_credentials_auth_binding", table_name=CREDENTIALS)
    op.drop_index("ix_user_credentials_party_id", table_name=CREDENTIALS)
    op.drop_constraint(
        "ck_user_credentials_party_binding_evidence", CREDENTIALS, type_="check"
    )
    op.drop_constraint("fk_user_credentials_tenant", CREDENTIALS, type_="foreignkey")
    op.drop_constraint(
        "fk_user_credentials_auth_binding", CREDENTIALS, type_="foreignkey"
    )
    op.drop_constraint("fk_user_credentials_party", CREDENTIALS, type_="foreignkey")
    for column in (
        "tenant_id",
        "authentication_binding_id",
        "party_binding_reason",
        "party_binding_source",
        "party_bound_at",
        "party_id",
    ):
        op.drop_column(CREDENTIALS, column)
    op.drop_index("ix_auth_bindings_code_active", table_name=BINDINGS)
    op.drop_table(BINDINGS)
