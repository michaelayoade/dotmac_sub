"""Add subscribers.crm_subscriber_id (direct CRM record link).

Revision ID: 131_add_crm_subscriber_id
Revises: 129_add_generic_sync_framework
Create Date: 2026-06-09

Additive. Stores the DotMac Omni CRM subscriber UUID directly so portal/CRM
lookups and the inbound ticket pull no longer depend on the
splynx_customer_id -> CRM external_id resolution chain.
"""

from __future__ import annotations

import sqlalchemy as sa
from sqlalchemy import inspect
from sqlalchemy.dialects.postgresql import UUID

from alembic import op

revision = "131_add_crm_subscriber_id"
down_revision = "129_add_generic_sync_framework"
branch_labels = None
depends_on = None

_TABLE = "subscribers"
_COL = "crm_subscriber_id"
_INDEX = "uq_subscribers_crm_subscriber_id"


def upgrade() -> None:
    bind = op.get_bind()
    # SQLite (tests) builds the schema from model metadata via create_all.
    if bind.dialect.name == "sqlite":
        return
    inspector = inspect(bind)
    cols = {c["name"] for c in inspector.get_columns(_TABLE)}
    if _COL not in cols:
        op.add_column(_TABLE, sa.Column(_COL, UUID(as_uuid=True), nullable=True))
    indexes = {i["name"] for i in inspector.get_indexes(_TABLE)}
    if _INDEX not in indexes:
        op.create_index(
            _INDEX,
            _TABLE,
            [_COL],
            unique=True,
            postgresql_where=sa.text(f"{_COL} IS NOT NULL"),
        )


def downgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name == "sqlite":
        return
    op.drop_index(_INDEX, table_name=_TABLE)
    op.drop_column(_TABLE, _COL)
