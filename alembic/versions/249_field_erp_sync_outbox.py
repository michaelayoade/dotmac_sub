"""ERP re-home PR 1: field_erp_sync_events outbox + sync_flow_ownership guard.

Creates the two substrate tables for the sub → DotMac ERP edge:

* ``field_erp_sync_events`` — the delivery outbox (unique idempotency key);
* ``sync_flow_ownership`` — the single-writer guard, seeded to ``crm`` for every
  flow (CRM keeps writing until each flow is cut over to sub).

Also adds the ``integration`` value to the ``settingdomain`` PG enum so the new
``dotmac_erp_*`` settings can be persisted (following the 225 precedent). The new
enum value is only ADDED here, never used in this migration, so it is safe in the
same transaction.

All DDL is idempotent (guarded by live-schema inspection) and sqlite
early-returns — the test harness builds the schema from model metadata via
``create_all``.

Revision ID: 249_field_erp_sync_outbox
Revises: 248_maps_vendor_route_domain
Create Date: 2026-07-11
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision = "249_field_erp_sync_outbox"
down_revision = "248_maps_vendor_route_domain"
branch_labels = None
depends_on = None

# Every flow seeded to CRM ownership (single-writer default).
_SEED_FLOWS = (
    "expense_claim",
    "material_request",
    "purchase_order",
    "purchase_invoice",
)


def _inspector():
    return sa.inspect(op.get_bind())


def _has_table(table_name: str) -> bool:
    return table_name in _inspector().get_table_names()


def _uuid_pk() -> sa.Column:
    return sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True)


def _create_field_erp_sync_events() -> None:
    if _has_table("field_erp_sync_events"):
        return
    op.create_table(
        "field_erp_sync_events",
        _uuid_pk(),
        sa.Column("flow", sa.String(length=40), nullable=False),
        sa.Column("entity_type", sa.String(length=80), nullable=False),
        sa.Column("entity_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("idempotency_key", sa.String(length=120), nullable=False),
        sa.Column("payload", sa.JSON(), nullable=False),
        sa.Column(
            "status", sa.String(length=20), nullable=False, server_default="pending"
        ),
        sa.Column("attempts", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("last_error", sa.Text()),
        sa.Column("erp_response", sa.JSON()),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("sent_at", sa.DateTime(timezone=True)),
        sa.UniqueConstraint(
            "idempotency_key", name="uq_field_erp_sync_events_idempotency_key"
        ),
    )
    op.create_index("ix_field_erp_sync_events_flow", "field_erp_sync_events", ["flow"])
    op.create_index(
        "ix_field_erp_sync_events_status", "field_erp_sync_events", ["status"]
    )


def _create_sync_flow_ownership() -> None:
    if _has_table("sync_flow_ownership"):
        return
    op.create_table(
        "sync_flow_ownership",
        _uuid_pk(),
        sa.Column("flow", sa.String(length=40), nullable=False),
        sa.Column("owner", sa.String(length=10), nullable=False, server_default="crm"),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_by", sa.String(length=120)),
        sa.UniqueConstraint("flow", name="uq_sync_flow_ownership_flow"),
    )


def _seed_flow_ownership() -> None:
    """Seed one CRM-owned row per flow (idempotent on the unique flow)."""
    ownership = sa.table(
        "sync_flow_ownership",
        sa.column("id", postgresql.UUID(as_uuid=True)),
        sa.column("flow", sa.String()),
        sa.column("owner", sa.String()),
        sa.column("updated_at", sa.DateTime(timezone=True)),
        sa.column("updated_by", sa.String()),
    )
    bind = op.get_bind()
    existing = {
        row[0] for row in bind.execute(sa.text("SELECT flow FROM sync_flow_ownership"))
    }
    now = datetime.now(UTC)
    rows = [
        {
            "id": uuid.uuid4(),
            "flow": flow,
            "owner": "crm",
            "updated_at": now,
            "updated_by": "migration:249_field_erp_sync_outbox",
        }
        for flow in _SEED_FLOWS
        if flow not in existing
    ]
    if rows:
        op.bulk_insert(ownership, rows)


def upgrade() -> None:
    if op.get_bind().dialect.name == "sqlite":
        return

    # Add the integration settings domain (only ADDED, never used here → safe in
    # this transaction). Mirrors the 225 precedent.
    op.execute("ALTER TYPE settingdomain ADD VALUE IF NOT EXISTS 'integration'")

    _create_field_erp_sync_events()
    _create_sync_flow_ownership()
    _seed_flow_ownership()


def downgrade() -> None:
    if op.get_bind().dialect.name == "sqlite":
        return

    for table_name in ("field_erp_sync_events", "sync_flow_ownership"):
        if _has_table(table_name):
            op.drop_table(table_name)
    # The 'integration' settingdomain enum value is left in place: PostgreSQL
    # enum values cannot be dropped without rebuilding the type, matching every
    # other enum migration's downgrade (see 225).
