"""Add add_ons.validity_days (data top-up validity window).

Revision ID: 127_add_addon_validity_days
Revises: 126_add_data_topup_fields
Create Date: 2026-06-09

Additive. null = the top-up expires at the end of its purchase period; N = valid
N days from purchase. Sourced from Splynx cap_tariff.validity.
"""

from __future__ import annotations

import sqlalchemy as sa
from sqlalchemy import inspect

from alembic import op

revision = "127_add_addon_validity_days"
down_revision = "126_add_data_topup_fields"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    cols = {c["name"] for c in inspect(bind).get_columns("add_ons")}
    if "validity_days" not in cols:
        op.add_column(
            "add_ons", sa.Column("validity_days", sa.Integer(), nullable=True)
        )


def downgrade() -> None:
    op.drop_column("add_ons", "validity_days")
