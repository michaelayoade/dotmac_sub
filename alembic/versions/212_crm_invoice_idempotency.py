"""Idempotency backstop for CRM-created installation invoices.

``create_installation_invoice`` deduped only via a select-then-insert on
``metadata->>'crm_external_ref'`` with no DB constraint, so a retried/concurrent
``POST /crm/invoices`` could create two invoices for one CRM charge.

Promotes the ref to a dedicated ``crm_external_ref`` column (a JSONB-expression
unique index isn't portable to the SQLite test suite), backfills it from the
existing metadata, then adds a partial unique index on active CRM rows. Aborts
loudly if duplicates already exist so a human reconciles them first.

Revision ID: 212_crm_invoice_idempotency
Revises: 211_seed_unmatched_radio_review_task
Create Date: 2026-07-05
"""

import sqlalchemy as sa
from sqlalchemy import inspect, text

from alembic import op

revision = "212_crm_invoice_idempotency"
down_revision = "211_seed_unmatched_radio_review_task"
branch_labels = None
depends_on = None

_TABLE = "invoices"
_COLUMN = "crm_external_ref"
_INDEX = "uq_invoices_active_crm_external_ref"


def _has_column(inspector, table: str, column: str) -> bool:
    if table not in inspector.get_table_names():
        return False
    return any(c["name"] == column for c in inspector.get_columns(table))


def _has_index(inspector, name: str) -> bool:
    if _TABLE not in inspector.get_table_names():
        return False
    return any(ix["name"] == name for ix in inspector.get_indexes(_TABLE))


def upgrade() -> None:
    bind = op.get_bind()
    inspector = inspect(bind)
    if _TABLE not in inspector.get_table_names():
        return

    if not _has_column(inspector, _TABLE, _COLUMN):
        op.add_column(_TABLE, sa.Column(_COLUMN, sa.String(length=120), nullable=True))
        # Backfill from the metadata the app has been writing.
        bind.execute(
            text(
                "UPDATE invoices SET crm_external_ref = metadata->>'crm_external_ref' "
                "WHERE crm_external_ref IS NULL "
                "AND metadata->>'crm_external_ref' IS NOT NULL"
            )
        )

    if _has_index(inspector, _INDEX):
        return

    dupes = bind.execute(
        text(
            "SELECT crm_external_ref, COUNT(*) AS n FROM invoices "
            "WHERE is_active AND crm_external_ref IS NOT NULL "
            "GROUP BY crm_external_ref HAVING COUNT(*) > 1"
        )
    ).fetchall()
    if dupes:
        preview = ", ".join(f"({row[0]}, n={row[1]})" for row in dupes[:20])
        raise RuntimeError(
            f"Cannot add {_INDEX}: {len(dupes)} duplicate CRM invoice "
            f"crm_external_ref(s) already exist. Reconcile them first (keep one, "
            f"deactivate the rest), then re-run. Offending: {preview}"
        )

    op.create_index(
        _INDEX,
        _TABLE,
        [_COLUMN],
        unique=True,
        postgresql_where=text("is_active AND crm_external_ref IS NOT NULL"),
        sqlite_where=text("is_active AND crm_external_ref IS NOT NULL"),
    )


def downgrade() -> None:
    bind = op.get_bind()
    inspector = inspect(bind)
    if _has_index(inspector, _INDEX):
        op.drop_index(_INDEX, table_name=_TABLE)
    if _has_column(inspector, _TABLE, _COLUMN):
        op.drop_column(_TABLE, _COLUMN)
