"""Idempotency backstop for CRM-originated payments.

CRM sales payments are recorded with ``external_id = 'crm:<ref>'`` and no
provider, so they sit outside ``uq_payments_active_external_id`` (which requires
``provider_id IS NOT NULL``). Without a DB backstop, a retried/concurrent
``POST /crm/payments`` push can double-record cash (the app-level
select-then-insert dedup has a race window).

Adds a partial unique index on ``external_id`` for active CRM rows. Aborts
loudly if duplicates already exist — a human reconciles them first rather than
the migration silently dropping payment records.

Revision ID: 210_crm_payment_idempotency
Revises: 209_add_uisp_topology_columns
Create Date: 2026-07-05
"""

from sqlalchemy import inspect, text

from alembic import op

revision = "210_crm_payment_idempotency"
down_revision = "209_add_uisp_topology_columns"
branch_labels = None
depends_on = None

_INDEX = "uq_payments_active_crm_external_id"
_TABLE = "payments"


def _has_index(inspector, name: str) -> bool:
    if _TABLE not in inspector.get_table_names():
        return False
    return any(ix["name"] == name for ix in inspector.get_indexes(_TABLE))


def upgrade() -> None:
    bind = op.get_bind()
    inspector = inspect(bind)
    if _TABLE not in inspector.get_table_names() or _has_index(inspector, _INDEX):
        return

    dupes = bind.execute(
        text(
            "SELECT external_id, COUNT(*) AS n FROM payments "
            "WHERE is_active AND external_id IS NOT NULL AND external_id LIKE 'crm:%' "
            "GROUP BY external_id HAVING COUNT(*) > 1"
        )
    ).fetchall()
    if dupes:
        preview = ", ".join(f"({row[0]}, n={row[1]})" for row in dupes[:20])
        raise RuntimeError(
            f"Cannot add {_INDEX}: {len(dupes)} duplicate CRM payment external_id(s) "
            f"already exist. Reconcile them first (keep one, deactivate the rest), "
            f"then re-run. Offending: {preview}"
        )

    op.create_index(
        _INDEX,
        _TABLE,
        ["external_id"],
        unique=True,
        postgresql_where=text(
            "is_active AND external_id IS NOT NULL AND external_id LIKE 'crm:%'"
        ),
    )


def downgrade() -> None:
    bind = op.get_bind()
    inspector = inspect(bind)
    if _has_index(inspector, _INDEX):
        op.drop_index(_INDEX, table_name=_TABLE)
