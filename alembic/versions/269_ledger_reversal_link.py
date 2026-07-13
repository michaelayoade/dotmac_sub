"""Link a reversal to the entry it reverses, and allow only one.

The ledger is append-only: an entry's effect is undone by posting its opposite.
``reversal_of_entry_id`` is the structural link between the two, and the unique
index makes a second reversal of the same entry impossible at the database level
— so the invariant no longer depends on every future caller remembering to take
the row lock first.

NO BACKFILL. Pre-existing reversals were only ever linked by memo text, and
inferring that pairing could pair the wrong rows — which, on a ledger, corrupts
money. They stay NULL and remain adjudicated by the service's legacy memo lookup
until a production pass says otherwise.

Revision ID: 269_ledger_reversal_link
Revises: 268_sot_safety_closure
"""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision = "269_ledger_reversal_link"
down_revision = "268_sot_safety_closure"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "ledger_entries",
        sa.Column("reversal_of_entry_id", sa.UUID(as_uuid=True), nullable=True),
    )
    op.create_foreign_key(
        "fk_ledger_entries_reversal_of_entry_id",
        "ledger_entries",
        "ledger_entries",
        ["reversal_of_entry_id"],
        ["id"],
        ondelete="RESTRICT",
    )
    # NULLs are distinct in a unique index, so ordinary entries are unconstrained
    # while every non-null reversal_of_entry_id is unique. Not scoped to
    # is_active: a deactivated reversal must still block a second reversal, or
    # deactivating it would silently re-open the double-post.
    op.create_index(
        "uq_ledger_entries_reversal_of",
        "ledger_entries",
        ["reversal_of_entry_id"],
        unique=True,
    )


def downgrade() -> None:
    op.drop_index("uq_ledger_entries_reversal_of", table_name="ledger_entries")
    op.drop_constraint(
        "fk_ledger_entries_reversal_of_entry_id",
        "ledger_entries",
        type_="foreignkey",
    )
    op.drop_column("ledger_entries", "reversal_of_entry_id")
