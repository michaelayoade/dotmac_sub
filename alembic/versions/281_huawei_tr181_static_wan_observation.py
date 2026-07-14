"""Persist observed DNS for reconciled static WANs.

Revision ID: 281_tr181_wan_observed_dns
Revises: 280_catalog_billing_write_permission
"""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision = "281_tr181_wan_observed_dns"
down_revision = "280_catalog_billing_write_permission"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "ont_observations",
        sa.Column("acs_observed_wan_dns_servers", sa.String(length=200)),
    )


def downgrade() -> None:
    op.drop_column("ont_observations", "acs_observed_wan_dns_servers")
