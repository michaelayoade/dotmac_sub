"""Give sales-order lines their own discount, matching quote lines.

Quote lines have carried ``discount_percent`` all along; sales-order lines
never did. ``create_from_quote`` copied the *net* ``amount`` across with
nowhere to record why it was net, and ``SalesOrderLines.update`` recomputes
``amount = quantity * unit_price`` — so the first edit to a discounted line
silently restored the gross price.

The backfill recovers the lost intent: where a line's stored amount is below
its own quantity x unit_price, that gap was a discount, so it is recorded as
one. This changes no amount; it makes the existing amount stable under a
future edit instead of springing back to gross.

Revision ID: 425_sales_order_line_discounts
Revises: 424_proposed_route_review_evidence
"""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision = "425_sales_order_line_discounts"
down_revision = "424_proposed_route_review_evidence"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "sales_order_lines",
        sa.Column(
            "discount_percent",
            sa.Numeric(precision=5, scale=2),
            nullable=False,
            server_default="0.00",
        ),
    )
    op.create_check_constraint(
        "ck_sales_order_lines_discount_percent_range",
        "sales_order_lines",
        "discount_percent >= 0 AND discount_percent <= 100",
    )
    op.execute(
        """
        UPDATE sales_order_lines
        SET discount_percent = LEAST(
            100,
            GREATEST(
                0,
                ROUND((1 - (amount / (quantity * unit_price))) * 100, 2)
            )
        )
        WHERE quantity IS NOT NULL
          AND unit_price IS NOT NULL
          AND amount IS NOT NULL
          AND quantity * unit_price > 0
          AND amount < quantity * unit_price
        """
    )


def downgrade() -> None:
    op.drop_constraint(
        "ck_sales_order_lines_discount_percent_range",
        "sales_order_lines",
        type_="check",
    )
    op.drop_column("sales_order_lines", "discount_percent")
