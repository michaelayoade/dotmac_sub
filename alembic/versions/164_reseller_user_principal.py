"""Layer 3 phase 0 — reseller_user as a first-class auth principal (additive).

Revision ID: 164_reseller_user_principal
Revises: 163_subscriber_email_non_unique
Create Date: 2026-06-20

Chained after #316's 163_subscriber_email_non_unique (live main head), giving a
single linear alembic head.

Additive only — no behaviour change. Reseller portal logins can become a
first-class `ResellerUser` principal instead of a fake `Subscriber`; the
dual-read auth code stays inert until `RESELLER_USER_PRINCIPAL_ENABLED` is on and
a backfill repoints reseller credentials. This adds:
  - reseller_user_id FK on user_credentials / mfa_methods / sessions
  - widens each table's "exactly one principal" CHECK from 2-way to 3-way
  - a primary-MFA-per-reseller_user partial unique index
  - identity columns on reseller_users (email, full_name, last_login_at)

Idempotent. The test suite builds its schema from models via
``Base.metadata.create_all`` and never runs this migration, so the CHECK swaps
here target Postgres prod (skipped on SQLite, which can't ALTER a CHECK in place).
"""

from __future__ import annotations

import sqlalchemy as sa
from sqlalchemy import inspect

from alembic import op

revision = "164_reseller_user_principal"
down_revision = "163_subscriber_email_non_unique"
branch_labels = None
depends_on = None

_PRINCIPAL_TABLES = ("user_credentials", "mfa_methods", "sessions")
_THREE_WAY = (
    "(CASE WHEN subscriber_id IS NOT NULL THEN 1 ELSE 0 END"
    " + CASE WHEN system_user_id IS NOT NULL THEN 1 ELSE 0 END"
    " + CASE WHEN reseller_user_id IS NOT NULL THEN 1 ELSE 0 END) = 1"
)


def _insp():
    return inspect(op.get_bind())


def _has_column(table: str, column: str) -> bool:
    return any(c["name"] == column for c in _insp().get_columns(table))


def _has_index(table: str, name: str) -> bool:
    return any(ix["name"] == name for ix in _insp().get_indexes(table))


def _check_names(table: str) -> set[str]:
    return {
        c["name"]
        for c in _insp().get_check_constraints(table)
        if c.get("name")
    }


def upgrade() -> None:
    bind = op.get_bind()
    is_pg = bind.dialect.name == "postgresql"

    # 1. Add reseller_user_id FK to each principal table.
    for table in _PRINCIPAL_TABLES:
        if not _has_column(table, "reseller_user_id"):
            op.add_column(
                table,
                sa.Column(
                    "reseller_user_id",
                    sa.dialects.postgresql.UUID(as_uuid=True)
                    if is_pg
                    else sa.String(36),
                    sa.ForeignKey("reseller_users.id"),
                    nullable=True,
                ),
            )

    # 2. Identity columns on reseller_users.
    if not _has_column("reseller_users", "email"):
        op.add_column("reseller_users", sa.Column("email", sa.String(255)))
    if not _has_column("reseller_users", "full_name"):
        op.add_column("reseller_users", sa.Column("full_name", sa.String(160)))
    if not _has_column("reseller_users", "last_login_at"):
        op.add_column(
            "reseller_users",
            sa.Column("last_login_at", sa.DateTime(timezone=True)),
        )

    # 3. Primary-MFA-per-reseller_user partial unique index.
    if not _has_index("mfa_methods", "ix_mfa_methods_primary_per_reseller_user"):
        op.create_index(
            "ix_mfa_methods_primary_per_reseller_user",
            "mfa_methods",
            ["reseller_user_id"],
            unique=True,
            postgresql_where=sa.text("is_primary"),
            sqlite_where=sa.text("is_primary"),
        )

    # 4. Widen the "exactly one principal" CHECK from 2-way to 3-way.
    #    Only on Postgres — SQLite cannot ALTER a CHECK without a table rebuild,
    #    and the test schema is built from the (already 3-way) models anyway.
    if is_pg:
        for table in _PRINCIPAL_TABLES:
            name = f"ck_{table}_exactly_one_principal"
            if name in _check_names(table):
                op.drop_constraint(name, table, type_="check")
            op.create_check_constraint(name, table, _THREE_WAY)


def downgrade() -> None:
    bind = op.get_bind()
    is_pg = bind.dialect.name == "postgresql"

    if is_pg:
        two_way = "(subscriber_id IS NOT NULL) <> (system_user_id IS NOT NULL)"
        for table in _PRINCIPAL_TABLES:
            name = f"ck_{table}_exactly_one_principal"
            if name in _check_names(table):
                op.drop_constraint(name, table, type_="check")
            op.create_check_constraint(name, table, two_way)

    if _has_index("mfa_methods", "ix_mfa_methods_primary_per_reseller_user"):
        op.drop_index("ix_mfa_methods_primary_per_reseller_user", "mfa_methods")

    for column in ("last_login_at", "full_name", "email"):
        if _has_column("reseller_users", column):
            op.drop_column("reseller_users", column)

    for table in _PRINCIPAL_TABLES:
        if _has_column(table, "reseller_user_id"):
            op.drop_column(table, "reseller_user_id")
