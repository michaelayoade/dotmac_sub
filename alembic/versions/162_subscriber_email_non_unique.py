"""Make subscribers.email non-unique (contact info, not identity).

Revision ID: 162_subscriber_email_non_unique
Revises: 159_ledger_effective_date
Create Date: 2026-06-20

DEPLOY NOTE — re-parent before applying to prod: this branch's alembic tree head
is 159, but prod/main has advanced (160 written_off, 161 IPAM-trend) which are
not in this working tree. Run ``alembic current`` / ``alembic heads`` INSIDE the
app container, then repoint ``down_revision`` below to the true prod head so this
revision applies after it (avoids a multi-head branch). Idempotent, so safe to
re-run after re-parenting.

Email overloaded three concepts: customer contact info, login identity, and
(historically) ownership. Ownership is already modelled via
``subscribers.reseller_id`` and login identity via ``user_credentials.username``
(unique per provider=local) / RADIUS. The only thing forcing artificial
``name+NNNN@host`` email mangling was this global ``UNIQUE(email)`` constraint.

This migration drops whichever object backs the uniqueness (a named unique
constraint and/or a unique index — environments differ) and replaces it with a
plain, non-unique index so lookups stay fast.

Idempotent and dialect-aware (the test suite builds its schema from models via
``Base.metadata.create_all`` and never runs this migration, so the careful
constraint introspection here targets Postgres prod).
"""

from __future__ import annotations

from sqlalchemy import inspect

from alembic import op

revision = "162_subscriber_email_non_unique"
down_revision = "159_ledger_effective_date"
branch_labels = None
depends_on = None

TABLE = "subscribers"
COLUMN = "email"
NEW_INDEX = "ix_subscribers_email"


def _unique_constraints_on_email(inspector) -> list[str]:
    return [
        uc["name"]
        for uc in inspector.get_unique_constraints(TABLE)
        if uc.get("name") and uc.get("column_names") == [COLUMN]
    ]


def _unique_indexes_on_email(inspector) -> list[str]:
    return [
        ix["name"]
        for ix in inspector.get_indexes(TABLE)
        if ix.get("name")
        and ix.get("unique")
        and ix.get("column_names") == [COLUMN]
    ]


def _has_index(inspector, name: str) -> bool:
    return any(ix.get("name") == name for ix in inspector.get_indexes(TABLE))


def upgrade() -> None:
    bind = op.get_bind()
    inspector = inspect(bind)

    # Drop the named unique constraint (Postgres: typically subscribers_email_key).
    for name in _unique_constraints_on_email(inspector):
        op.drop_constraint(name, TABLE, type_="unique")

    # Drop any unique index backing email (some envs realise uniqueness this way).
    for name in _unique_indexes_on_email(inspector):
        if name != NEW_INDEX:
            op.drop_index(name, table_name=TABLE)

    # Re-inspect: the dropped unique index may have been named ix_subscribers_email
    # already; ensure a *non-unique* index exists for lookup performance.
    inspector = inspect(bind)
    if not _has_index(inspector, NEW_INDEX):
        op.create_index(NEW_INDEX, TABLE, [COLUMN], unique=False)


def downgrade() -> None:
    # Best-effort, effectively one-way: re-adding UNIQUE fails if duplicate
    # contact emails now exist (which is the whole point of this change).
    bind = op.get_bind()
    inspector = inspect(bind)
    if _has_index(inspector, NEW_INDEX):
        op.drop_index(NEW_INDEX, table_name=TABLE)
    op.create_unique_constraint("subscribers_email_key", TABLE, [COLUMN])
