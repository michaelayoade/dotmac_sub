"""ERP re-home PR 3: material-request ERP mirror fields.

Adds the ERP write-back / idempotency columns to ``field_material_requests`` so
the material-request ISSUE flow can mirror the shape FieldExpenseRequest already
carries:

* ``erp_material_request_id`` — ERP's request id, written back once ERP accepts
  the ISSUE (indexed for the status-reconcile lookup);
* ``erp_material_status`` — the last ERP-reported fulfillment/stock status;
* ``client_ref`` — create idempotency token (mobile retry-safety), unique like
  the expense counterpart.

All DDL is idempotent (guarded by live-schema inspection) and sqlite
early-returns — the test harness builds the schema from model metadata via
``create_all``. INERT: the columns are unused in prod until the material_request
flow is cut over to sub (sync_flow_ownership stays 'crm').

Revision ID: 250_field_material_request_erp_fields
Revises: 249_field_erp_sync_outbox
Create Date: 2026-07-11
"""

from __future__ import annotations

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision = "250_field_material_request_erp_fields"
down_revision = "249_field_erp_sync_outbox"
branch_labels = None
depends_on = None

_TABLE = "field_material_requests"


def _inspector():
    return sa.inspect(op.get_bind())


def _has_table(table_name: str) -> bool:
    return table_name in _inspector().get_table_names()


def _has_column(table_name: str, column_name: str) -> bool:
    return column_name in {col["name"] for col in _inspector().get_columns(table_name)}


def _has_index(table_name: str, index_name: str) -> bool:
    return index_name in {ix["name"] for ix in _inspector().get_indexes(table_name)}


def upgrade() -> None:
    if op.get_bind().dialect.name == "sqlite":
        return
    if not _has_table(_TABLE):
        return

    if not _has_column(_TABLE, "erp_material_request_id"):
        op.add_column(
            _TABLE, sa.Column("erp_material_request_id", sa.String(length=120))
        )
    if not _has_column(_TABLE, "erp_material_status"):
        op.add_column(_TABLE, sa.Column("erp_material_status", sa.String(length=40)))
    if not _has_column(_TABLE, "client_ref"):
        op.add_column(_TABLE, sa.Column("client_ref", postgresql.UUID(as_uuid=True)))

    if not _has_index(_TABLE, "ix_field_material_requests_erp_material_request_id"):
        op.create_index(
            "ix_field_material_requests_erp_material_request_id",
            _TABLE,
            ["erp_material_request_id"],
        )
    if not _has_index(_TABLE, "ix_field_material_requests_client_ref"):
        op.create_index(
            "ix_field_material_requests_client_ref",
            _TABLE,
            ["client_ref"],
            unique=True,
        )


def downgrade() -> None:
    if op.get_bind().dialect.name == "sqlite":
        return
    if not _has_table(_TABLE):
        return

    if _has_index(_TABLE, "ix_field_material_requests_client_ref"):
        op.drop_index("ix_field_material_requests_client_ref", table_name=_TABLE)
    if _has_index(_TABLE, "ix_field_material_requests_erp_material_request_id"):
        op.drop_index(
            "ix_field_material_requests_erp_material_request_id", table_name=_TABLE
        )
    for column_name in ("client_ref", "erp_material_status", "erp_material_request_id"):
        if _has_column(_TABLE, column_name):
            op.drop_column(_TABLE, column_name)
