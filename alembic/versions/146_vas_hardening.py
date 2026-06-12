"""VAS hardening: reseller attribution + rate snapshot on transactions.

Designed in ahead of Phase 3 (reseller commissions) so transactions never
need a backfill: reseller_id stamps the initiator at purchase time; the
rate-snapshot columns stay NULL until the commission engine fills them.

Revision ID: 146_vas_hardening
Revises: 145_vas_catalog_transactions
Create Date: 2026-06-12
"""

from __future__ import annotations

import sqlalchemy as sa
from sqlalchemy import inspect
from sqlalchemy.dialects.postgresql import UUID

from alembic import op

revision = "146_vas_hardening"
down_revision = "145_vas_catalog_transactions"
branch_labels = None
depends_on = None

_COLUMNS = (
    sa.Column("reseller_id", UUID(as_uuid=True), sa.ForeignKey("resellers.id")),
    sa.Column("vtpass_rate_pct", sa.Numeric(7, 4)),
    sa.Column("reseller_rate_pct", sa.Numeric(7, 4)),
    sa.Column("owner_net", sa.Numeric(12, 2)),
)


def upgrade() -> None:
    bind = op.get_bind()
    existing = {
        column["name"] for column in inspect(bind).get_columns("vas_transactions")
    }
    for column in _COLUMNS:
        if column.name not in existing:
            op.add_column("vas_transactions", column.copy())


def downgrade() -> None:
    bind = op.get_bind()
    existing = {
        column["name"] for column in inspect(bind).get_columns("vas_transactions")
    }
    for column in reversed(_COLUMNS):
        if column.name in existing:
            op.drop_column("vas_transactions", column.name)
