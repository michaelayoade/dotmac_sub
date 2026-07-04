"""Add FDH cabinet target to outage incidents.

Revision ID: 206_add_fdh_target_to_outage_incidents
Revises: 205_add_crm_webhook_deliveries
Create Date: 2026-07-04
"""

from __future__ import annotations

import sqlalchemy as sa
from sqlalchemy import inspect
from sqlalchemy.dialects.postgresql import UUID

from alembic import op

revision = "206_add_fdh_target_to_outage_incidents"
down_revision = "205_add_crm_webhook_deliveries"
branch_labels = None
depends_on = None

TABLE = "outage_incidents"
COLUMN = "fdh_cabinet_id"
INDEX = "ix_outage_incidents_fdh_cabinet"


def _has_column(table: str, column: str) -> bool:
    inspector = inspect(op.get_bind())
    return any(col["name"] == column for col in inspector.get_columns(table))


def _has_index(table: str, index_name: str) -> bool:
    inspector = inspect(op.get_bind())
    return any(index["name"] == index_name for index in inspector.get_indexes(table))


def upgrade() -> None:
    if not _has_column(TABLE, COLUMN):
        op.add_column(
            TABLE,
            sa.Column(
                COLUMN,
                UUID(as_uuid=True),
                sa.ForeignKey("fdh_cabinets.id"),
                nullable=True,
            ),
        )
    if not _has_index(TABLE, INDEX):
        op.create_index(INDEX, TABLE, [COLUMN], unique=False)


def downgrade() -> None:
    if _has_index(TABLE, INDEX):
        op.drop_index(INDEX, table_name=TABLE)
    if _has_column(TABLE, COLUMN):
        op.drop_column(TABLE, COLUMN)
