"""Add the dormant released accounting-fact transport flow.

Revision ID: 688_billing_accounting_fact_export
Revises: 686_field_lifecycle_router_health_merge

Additive enum label only; no rows, transport activation or Billing authority move.
The actual predecessor is the terminal graph revision, not the highest filename.
PostgreSQL serializes enum catalogue changes; lock budget 5s, statement budget
30s, no table rewrite and no data-volume dependency. Failure rolls back the
migration; retry the same upgrade after contention clears. Forward-only: enum
labels cannot be removed safely while historical export rows can refer to them.
"""

from __future__ import annotations

from alembic import op

revision = "688_billing_accounting_fact_export"
down_revision = "686_field_lifecycle_router_health_merge"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("SET LOCAL lock_timeout = '5s'")
    op.execute("SET LOCAL statement_timeout = '30s'")
    op.execute(
        "ALTER TYPE public.erpbillingflow ADD VALUE IF NOT EXISTS 'accounting_fact'"
    )


def downgrade() -> None:
    """Keep the additive label and all historical evidence; no destructive inverse."""
