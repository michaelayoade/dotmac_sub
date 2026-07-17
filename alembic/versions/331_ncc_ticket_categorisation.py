"""Store the NCC complaints classification on tickets, and backfill it.

The NCC quarterly complaints return (①) files a Category and sub-category per
complaint. CRM derived both by keyword-matching free text at report time and
stored nothing, so nobody could correct a mis-classification and the filed
numbers moved whenever the rules moved. The classification is now derived on
save and stored (see app/services/ncc_categorisation.py), with a ``*_source``
of "derived" or "agent"; an agent value is never re-derived.

The backfill applies the same rules once to existing tickets, reproducing what
CRM would have filed. From then on, agent corrections accumulate.

Idempotent: only fills rows where ``ncc_category IS NULL``, so re-running is a
no-op and a later re-run can never overwrite an agent's correction.

Revision ID: 331_ncc_ticket_categorisation
Revises: 328_work_order_native_identity
"""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision = "331_ncc_ticket_categorisation"
down_revision = "328_work_order_native_identity"
branch_labels = None
depends_on = None

_COLUMNS = (
    ("ncc_category", sa.String(80)),
    ("ncc_category_source", sa.String(16)),
    ("ncc_subcategory", sa.String(120)),
    ("ncc_subcategory_source", sa.String(16)),
)

_BACKFILL_BATCH = 1000


def upgrade() -> None:
    for name, column_type in _COLUMNS:
        op.add_column("support_tickets", sa.Column(name, column_type, nullable=True))

    conn = op.get_bind()
    pending = conn.execute(
        sa.text("SELECT count(*) FROM support_tickets WHERE ncc_category IS NULL")
    ).scalar()
    if not pending:
        # Fresh database (001_squashed builds the columns from the model, so
        # there is nothing to backfill). Skip before importing app code: a
        # migration that imports a service it does not need is a migration
        # that breaks when that service moves.
        return

    from app.services.ncc_categorisation import SOURCE_DERIVED, derive_for

    rows = conn.execute(
        sa.text(
            "SELECT id, ticket_type, title, description FROM support_tickets "
            "WHERE ncc_category IS NULL"
        )
    ).fetchall()

    updates = [
        {
            "ticket_id": row.id,
            "category": category,
            "subcategory": subcategory,
            "source": SOURCE_DERIVED,
        }
        for row in rows
        for category, subcategory in [
            derive_for(
                ticket_type=row.ticket_type,
                subject=row.title,
                description=row.description,
            )
        ]
    ]

    statement = sa.text(
        "UPDATE support_tickets SET ncc_category = :category, "
        "ncc_category_source = :source, ncc_subcategory = :subcategory, "
        "ncc_subcategory_source = :source WHERE id = :ticket_id "
        "AND ncc_category IS NULL"
    )
    for start in range(0, len(updates), _BACKFILL_BATCH):
        conn.execute(statement, updates[start : start + _BACKFILL_BATCH])


def downgrade() -> None:
    for name, _column_type in reversed(_COLUMNS):
        op.drop_column("support_tickets", name)
