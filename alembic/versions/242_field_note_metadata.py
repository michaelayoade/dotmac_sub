"""Add provenance metadata to field work-order notes.

Phase 2 (sub = work-order system-of-record) backfill support: notes imported
from CRM ``work_order_notes`` carry ``{"source": "crm", "crm_note_id": ...}``
so the importer can dedupe on the CRM note id and operators can tell imported
notes from native ones. Native notes leave the column NULL.

Revision ID: 242_field_note_metadata
Revises: 241_service_entitlement_ledger_source
Create Date: 2026-07-10
"""

from __future__ import annotations

import sqlalchemy as sa
from sqlalchemy import inspect

from alembic import op

revision = "242_field_note_metadata"
down_revision = "241_service_entitlement_ledger_source"
branch_labels = None
depends_on = None

_TABLE = "field_work_order_notes"
_COLUMN = "metadata"


def _has_table(name: str) -> bool:
    return name in inspect(op.get_bind()).get_table_names()


def _has_column(table: str, column: str) -> bool:
    return column in {col["name"] for col in inspect(op.get_bind()).get_columns(table)}


def upgrade() -> None:
    if not _has_table(_TABLE) or _has_column(_TABLE, _COLUMN):
        return
    op.add_column(_TABLE, sa.Column(_COLUMN, sa.JSON(), nullable=True))


def downgrade() -> None:
    if not _has_table(_TABLE) or not _has_column(_TABLE, _COLUMN):
        return
    op.drop_column(_TABLE, _COLUMN)
