"""Add data-top-up fields: add_ons.grant_gb + quota_buckets.topup_gb.

Revision ID: 126_add_data_topup_fields
Revises: 125_add_usage_allowance_rollover
Create Date: 2026-06-09

Additive. grant_gb marks a data-top-up add-on (GB granted on purchase);
topup_gb accumulates granted GB on the period's quota bucket.
"""

from __future__ import annotations

import sqlalchemy as sa
from sqlalchemy import inspect

from alembic import op

revision = "126_add_data_topup_fields"
down_revision = "125_add_usage_allowance_rollover"
branch_labels = None
depends_on = None


def _has(bind, table, col) -> bool:
    return col in {c["name"] for c in inspect(bind).get_columns(table)}


def upgrade() -> None:
    bind = op.get_bind()
    if not _has(bind, "add_ons", "grant_gb"):
        op.add_column("add_ons", sa.Column("grant_gb", sa.Integer(), nullable=True))
    if not _has(bind, "quota_buckets", "topup_gb"):
        op.add_column(
            "quota_buckets",
            sa.Column(
                "topup_gb",
                sa.Numeric(10, 2),
                nullable=False,
                server_default="0",
            ),
        )


def downgrade() -> None:
    op.drop_column("quota_buckets", "topup_gb")
    op.drop_column("add_ons", "grant_gb")
