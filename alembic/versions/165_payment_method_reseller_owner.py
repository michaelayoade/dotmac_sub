"""Allow payment_methods to be owned by a reseller org (Layer 3 #329).

Revision ID: 165_payment_method_reseller_owner
Revises: 164_reseller_user_principal
Create Date: 2026-06-20

Reseller saved cards were keyed on the reseller's login Subscriber
(``payment_methods.account_id``). A first-class ``reseller_user`` principal
(Layer 3) has no backing subscriber, so this adds an alternative owner: the
reseller org (``reseller_id``). Additive — ``account_id`` becomes nullable and a
CHECK enforces exactly one owner. Existing rows (all account-owned) satisfy it.

Principal-routed, no data migration: existing subscriber-backed reseller cards
stay on ``account_id``; only reseller_user logins use ``reseller_id``. (Moving
legacy reseller cards to reseller_id is an optional later cleanup.)

Idempotent. The test suite builds its schema from models via
``Base.metadata.create_all`` and never runs this migration, so the nullable/CHECK
DDL here targets Postgres prod (skipped on SQLite, which can't ALTER in place).
"""

from __future__ import annotations

import sqlalchemy as sa
from sqlalchemy import inspect

from alembic import op

revision = "165_payment_method_reseller_owner"
down_revision = "164_reseller_user_principal"
branch_labels = None
depends_on = None

TABLE = "payment_methods"
COLUMN = "reseller_id"
CK = "ck_payment_methods_exactly_one_owner"


def _insp():
    return inspect(op.get_bind())


def _has_column(table: str, column: str) -> bool:
    return any(c["name"] == column for c in _insp().get_columns(table))


def _check_names(table: str) -> set[str]:
    return {c["name"] for c in _insp().get_check_constraints(table) if c.get("name")}


def upgrade() -> None:
    bind = op.get_bind()
    is_pg = bind.dialect.name == "postgresql"

    col_type: sa.types.TypeEngine = (
        sa.dialects.postgresql.UUID(as_uuid=True) if is_pg else sa.String(36)
    )
    if not _has_column(TABLE, COLUMN):
        op.add_column(
            TABLE,
            sa.Column(
                COLUMN,
                col_type,
                sa.ForeignKey("resellers.id"),
                nullable=True,
            ),
        )

    if is_pg:
        op.alter_column(
            TABLE,
            "account_id",
            existing_type=sa.dialects.postgresql.UUID(as_uuid=True),
            nullable=True,
        )
        if CK not in _check_names(TABLE):
            op.create_check_constraint(
                CK,
                TABLE,
                "(CASE WHEN account_id IS NOT NULL THEN 1 ELSE 0 END"
                " + CASE WHEN reseller_id IS NOT NULL THEN 1 ELSE 0 END) = 1",
            )


def downgrade() -> None:
    bind = op.get_bind()
    is_pg = bind.dialect.name == "postgresql"
    if is_pg:
        if CK in _check_names(TABLE):
            op.drop_constraint(CK, TABLE, type_="check")
        # Best-effort: re-tightening account_id to NOT NULL fails if any
        # reseller-owned (account_id NULL) rows exist.
        op.alter_column(
            TABLE,
            "account_id",
            existing_type=sa.dialects.postgresql.UUID(as_uuid=True),
            nullable=False,
        )
    if _has_column(TABLE, COLUMN):
        op.drop_column(TABLE, COLUMN)
