"""Add explicit VAT treatment to catalog prices.

Revision ID: 640_catalog_price_tax_application
Revises: 639_machine_attribution
Create Date: 2026-10-02

Existing prices are backfilled as exclusive because that exactly preserves the
pre-migration invoice behavior. Gross prices must be reviewed and deliberately
changed to inclusive after deployment; the migration does not infer commercial
intent from rounded amounts.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "640_catalog_price_tax_application"
down_revision: str | None = "639_machine_attribution"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _tax_application_enum() -> postgresql.ENUM:
    return postgresql.ENUM(
        "exclusive",
        "inclusive",
        "exempt",
        name="taxapplication",
        create_type=False,
    )


def upgrade() -> None:
    for table_name in ("offer_prices", "offer_version_prices", "add_on_prices"):
        op.add_column(
            table_name,
            sa.Column(
                "tax_application",
                _tax_application_enum(),
                nullable=False,
                server_default=sa.text("'exclusive'::taxapplication"),
            ),
        )


def downgrade() -> None:
    for table_name in ("add_on_prices", "offer_version_prices", "offer_prices"):
        op.drop_column(table_name, "tax_application")
