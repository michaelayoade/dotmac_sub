"""Backfill legacy total-only credit-note headers to the balance invariant.

The Splynx import (and a handful of voided native-era rows) stored credit
notes with only ``total`` populated: ``subtotal = 0``, ``tax_total = 0``,
``total > 0`` and zero credit_note_lines — 2,138 of 2,145 rows in prod as of
2026-07-18. Downstream projections (dotmac_erp ``_project_source_lines``)
correctly refuse headers where ``subtotal + tax_total != total``, so the
whole legacy population failed erp credit-note sync.

Accounting standard (Michael, 2026-07-18): credit notes are not VAT
transactions — the balanced form is ``subtotal = total, tax_total = 0``.

The WHERE targets exactly the total-only defect shape, so the repair is
idempotent and cannot touch balanced or partially-populated rows. Applied
directly to prod on 2026-07-18 (2,138 rows); this migration re-asserts it
for every other environment and no-ops where already clean.

Revision ID: 346_credit_note_legacy_balance_backfill
Revises: 345_fiber_topology_field_observations
Create Date: 2026-07-18
"""

from __future__ import annotations

from alembic import op

revision = "346_credit_note_legacy_balance_backfill"
down_revision = "345_fiber_topology_field_observations"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        UPDATE credit_notes
        SET subtotal = total, tax_total = 0.00
        WHERE subtotal = 0 AND tax_total = 0 AND total <> 0
        """
    )


def downgrade() -> None:
    # Data repair: the pre-backfill zeros carried no information worth
    # restoring, and reverting would re-break the balance invariant.
    pass
