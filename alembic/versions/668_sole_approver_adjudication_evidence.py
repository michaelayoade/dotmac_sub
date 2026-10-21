"""Carried-source adjudication: sole-approver exception evidence (expand step).

Revision ID: 668_sole_approver_adjudication_evidence
Revises: 667_captive_access_policy_backfill
Create Date: 2026-10-10

## Why

``governance.sole_approver_exception`` lets the named sole decision-maker
approve their own carried-source identity review while a time-boxed Governance
exception is in force. The adjudication row refused ``reviewed_by_id =
approved_by_id`` outright, so the row needs somewhere to carry the exception
evidence, and the distinct-reviewers CHECK must admit equality ONLY when that
evidence is present.

## What it does (additive; the table is append-only and no row is touched)

1. ``sole_approver_exception`` boolean NOT NULL, default false (metadata-only
   in PostgreSQL 11+), plus nullable ``sole_approver_exception_ref`` and
   ``sole_approver_justification``.
2. Replaces ``ck_carried_source_identity_distinct_reviewers`` with
   ``reviewed_by_id <> approved_by_id OR sole_approver_exception``. Existing
   rows are all distinct, so validation cannot fail.
3. Adds ``ck_carried_source_identity_sole_approver_evidence``: an exception row
   must carry a non-empty decision reference and justification.

Downgrade restores the original CHECK and drops the columns; it refuses if an
exception row exists, because the original constraint could not hold it.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "668_sole_approver_adjudication_evidence"
down_revision: str | None = "667_captive_access_policy_backfill"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_TABLE = "carried_source_identity_adjudications"
_DISTINCT = "ck_carried_source_identity_distinct_reviewers"
_EVIDENCE = "ck_carried_source_identity_sole_approver_evidence"


def upgrade() -> None:
    op.execute("SET LOCAL lock_timeout = '5s'")
    op.add_column(
        _TABLE,
        sa.Column(
            "sole_approver_exception",
            sa.Boolean(),
            nullable=False,
            server_default=sa.false(),
        ),
    )
    op.add_column(
        _TABLE, sa.Column("sole_approver_exception_ref", sa.String(240), nullable=True)
    )
    op.add_column(
        _TABLE, sa.Column("sole_approver_justification", sa.Text(), nullable=True)
    )
    op.drop_constraint(_DISTINCT, _TABLE, type_="check")
    op.create_check_constraint(
        _DISTINCT, _TABLE, "reviewed_by_id <> approved_by_id OR sole_approver_exception"
    )
    op.create_check_constraint(
        _EVIDENCE,
        _TABLE,
        "NOT sole_approver_exception OR ("
        "length(trim(coalesce(sole_approver_exception_ref, ''))) > 0 AND "
        "length(trim(coalesce(sole_approver_justification, ''))) > 0)",
    )


def downgrade() -> None:
    op.execute("SET LOCAL lock_timeout = '5s'")
    bind = op.get_bind()
    used = bind.execute(
        sa.text(f"SELECT 1 FROM {_TABLE} WHERE sole_approver_exception LIMIT 1")
    ).first()
    if used is not None:
        raise RuntimeError(
            "Refusing to downgrade: a sole-approver exception adjudication exists "
            "and the original distinct-reviewers constraint could not hold it."
        )
    op.drop_constraint(_EVIDENCE, _TABLE, type_="check")
    op.drop_constraint(_DISTINCT, _TABLE, type_="check")
    op.create_check_constraint(_DISTINCT, _TABLE, "reviewed_by_id <> approved_by_id")
    op.drop_column(_TABLE, "sole_approver_justification")
    op.drop_column(_TABLE, "sole_approver_exception_ref")
    op.drop_column(_TABLE, "sole_approver_exception")
