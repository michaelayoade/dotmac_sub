"""Persist complete-cycle outcome totals for the bounded prepaid sweep.

The prepaid sweep is a bounded keyset cycle: one cycle can span several
budget-limited runs. Its per-account outcome classes (renewal terms
unresolved, coverage unresolved, notice suppressed, ...) were published per
run, so a run that covered only a slice of the cohort exported a partial
count as if it were the total. These columns let the sweep tally outcomes per
account across the runs of one cycle and publish only complete-cycle totals.

Purely additive. ``cycle_outcomes`` is deliberately left NULL on the existing
row: NULL means "the in-progress cycle began before tallying existed", so the
sweep never publishes a partial first cycle as complete; tallying starts with
the next fresh cycle. No backfill is required. Downgrade drops the columns;
the data is a rebuildable projection of the next completed cycle.

Revision ID: 651_prepaid_sweep_cycle_totals
Revises: 650_customer_connection_type
Create Date: 2026-10-08
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "651_prepaid_sweep_cycle_totals"
down_revision: str | None = "650_customer_connection_type"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_TABLE = "prepaid_sweep_cycle_state"


def upgrade() -> None:
    if op.get_bind().dialect.name == "postgresql":
        op.execute("SET LOCAL lock_timeout = '5s'")
    op.add_column(_TABLE, sa.Column("cycle_outcomes", sa.JSON(), nullable=True))
    op.add_column(_TABLE, sa.Column("last_cycle_totals", sa.JSON(), nullable=True))
    op.add_column(
        _TABLE,
        sa.Column("last_cycle_completed_at", sa.DateTime(timezone=True), nullable=True),
    )


def downgrade() -> None:
    op.drop_column(_TABLE, "last_cycle_completed_at")
    op.drop_column(_TABLE, "last_cycle_totals")
    op.drop_column(_TABLE, "cycle_outcomes")
