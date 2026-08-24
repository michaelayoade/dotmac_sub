"""Create `machine_credentials` — the kernel's tenant-scoped `X-Api-Key` row.

Sub writes this migration itself rather than composing the kernel's Alembic
lineage, for the reason `508_operator_tenant_tables` records: kernel revision
`0004_custom_fields` adds a column to `parties`, and Sub has its own `parties`
table, so composing the lineages would alter a live Sub table. The column
definitions therefore mirror `dotmac_kernel.machine_models.MachineCredential`
exactly, because the kernel's ORM reads these rows.

Expand only. Nothing writes here yet and Sub's existing `api_keys` path is
untouched: the guard switch and the credential reissue are separate changes.
That ordering is deliberate — a table with no rows changes no behaviour, so
this can land and be verified on its own.

## Why the reissue cannot be avoided

`machine_auth` derives its HMAC from a DEDICATED held secret
(`machine_credential_hmac_key`), not from the connector-credential Fernet key
Sub's `_api_key_hmac_secret` reuses. A stored digest holds neither the raw key
nor material to re-key from, so no existing `api_keys` row can be converted.
Both live credentials are issued fresh or not at all.

## The constraints are the point

`scopes` is NOT NULL with no default, so a row cannot exist without saying what
it may do — ERP's nullable column and everything-grants default is the defect
this refuses. `key_hash LIKE 'hmac-sha256:%'` is CHECKed, so the unsalted
SHA-256 form Sub still accepts on the old path cannot appear here. There is no
`last_used_at`, so authentication cannot write during a read. There is no human
FK, because a machine principal is not a person.

`key_hash` is unique WITH `tenant_id` rather than globally: the tenant is
established before the lookup, so RLS already leaves one candidate, and a
global constraint would let one tenant's key collide with a row another tenant
cannot see — a denial and a disclosure at once.

RLS is ENABLEd *and* FORCEd. Without FORCE the table owner, which migrations
run as, bypasses its own policy.

Revision ID: 551_machine_credentials
Revises: 550_integrator_provider_ref
"""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision = "551_machine_credentials"
down_revision = "550_integrator_provider_ref"
branch_labels = None
depends_on = None

_TABLE = "machine_credentials"


def upgrade() -> None:
    op.create_table(
        _TABLE,
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("tenant_id", sa.Uuid(), nullable=False),
        sa.Column("label", sa.String(120), nullable=False),
        sa.Column("key_hash", sa.String(120), nullable=False),
        sa.Column("scopes", sa.JSON(), nullable=False),
        sa.Column(
            "is_active", sa.Boolean(), nullable=False, server_default=sa.text("true")
        ),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id"],
            ["tenants.id"],
            ondelete="CASCADE",
            name="fk_machine_credentials_tenant",
        ),
        sa.UniqueConstraint(
            "tenant_id", "key_hash", name="uq_machine_credentials_tenant_key_hash"
        ),
        sa.UniqueConstraint(
            "tenant_id", "label", name="uq_machine_credentials_tenant_label"
        ),
        sa.CheckConstraint(
            "length(trim(label)) > 0", name="ck_machine_credentials_label_nonempty"
        ),
        sa.CheckConstraint(
            "key_hash LIKE 'hmac-sha256:%'",
            name="ck_machine_credentials_key_hash_scheme",
        ),
    )
    op.create_index(f"ix_{_TABLE}_tenant_id", _TABLE, ["tenant_id"])
    op.execute(f"ALTER TABLE {_TABLE} ENABLE ROW LEVEL SECURITY;")
    op.execute(f"ALTER TABLE {_TABLE} FORCE ROW LEVEL SECURITY;")
    op.execute(
        f"""
        CREATE POLICY {_TABLE}_tenant_isolation ON {_TABLE}
          USING (tenant_id = app_current_tenant_id())
          WITH CHECK (tenant_id = app_current_tenant_id());
        """
    )
    op.execute(f"GRANT SELECT, INSERT, UPDATE, DELETE ON {_TABLE} TO app_user;")
    op.execute(f"GRANT SELECT, INSERT, UPDATE, DELETE ON {_TABLE} TO platform_api;")


def downgrade() -> None:
    op.execute(f"DROP POLICY IF EXISTS {_TABLE}_tenant_isolation ON {_TABLE};")
    op.drop_index(f"ix_{_TABLE}_tenant_id", table_name=_TABLE)
    op.drop_table(_TABLE)
