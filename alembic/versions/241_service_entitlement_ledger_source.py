"""Add ledger source key to service entitlements.

Revision ID: 241_service_entitlement_ledger_source
Revises: 240_add_service_entitlements
Create Date: 2026-07-10
"""

from __future__ import annotations

import sqlalchemy as sa
from sqlalchemy import inspect
from sqlalchemy.dialects import postgresql

from alembic import op

revision = "241_service_entitlement_ledger_source"
down_revision = "240_add_service_entitlements"
branch_labels = None
depends_on = None

_TABLE = "service_entitlements"
_COLUMN = "source_ledger_entry_id"
_INDEX = "uq_service_entitlements_active_ledger_entry"


def _has_table(name: str) -> bool:
    return name in inspect(op.get_bind()).get_table_names()


def _has_column(table: str, column: str) -> bool:
    return column in {col["name"] for col in inspect(op.get_bind()).get_columns(table)}


def upgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name == "sqlite" or not _has_table(_TABLE):
        return
    if not _has_column(_TABLE, _COLUMN):
        op.add_column(
            _TABLE,
            sa.Column(
                _COLUMN,
                postgresql.UUID(as_uuid=True),
                sa.ForeignKey("ledger_entries.id"),
            ),
        )
    op.execute(
        sa.text(
            """
            WITH candidates AS (
                SELECT id,
                       (metadata ->> 'source_ledger_entry_id')::uuid AS ledger_id,
                       row_number() OVER (
                           PARTITION BY metadata ->> 'source_ledger_entry_id'
                           ORDER BY created_at ASC, id ASC
                       ) AS rn
                  FROM service_entitlements
                 WHERE source_ledger_entry_id IS NULL
                   AND status = 'active'
                   AND metadata ? 'source_ledger_entry_id'
                   AND metadata ->> 'source_ledger_entry_id' ~
                       '^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$'
            )
            UPDATE service_entitlements se
               SET source_ledger_entry_id = candidates.ledger_id
              FROM candidates
             WHERE se.id = candidates.id
               AND candidates.rn = 1
            """
        )
    )
    op.create_index(
        _INDEX,
        _TABLE,
        [_COLUMN],
        unique=True,
        postgresql_where=sa.text(
            "status = 'active' AND source_ledger_entry_id IS NOT NULL"
        ),
    )


def downgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name == "sqlite" or not _has_table(_TABLE):
        return
    op.drop_index(_INDEX, table_name=_TABLE)
    if _has_column(_TABLE, _COLUMN):
        op.drop_column(_TABLE, _COLUMN)
