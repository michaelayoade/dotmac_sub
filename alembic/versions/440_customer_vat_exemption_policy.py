"""Add customer VAT exemption policy.

Revision ID: 440_customer_vat_exemption_policy
Revises: 439_billing_obligation_rating_provenance
"""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision = "440_customer_vat_exemption_policy"
down_revision = "439_billing_obligation_rating_provenance"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "customer_tax_policies",
        sa.Column(
            "vat_exempt",
            sa.Boolean(),
            nullable=False,
            server_default=sa.false(),
        ),
    )


def downgrade() -> None:
    op.drop_column("customer_tax_policies", "vat_exempt")
