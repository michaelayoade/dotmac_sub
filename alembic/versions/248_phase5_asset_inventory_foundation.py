"""Phase 5 asset inventory foundation.

Revision ID: 248_phase5_asset_inventory
Revises: 247_merge_phase3_inbox_heads
Create Date: 2026-07-11
"""

from __future__ import annotations

import sqlalchemy as sa
from sqlalchemy import inspect
from sqlalchemy.dialects import postgresql

from alembic import op

revision = "248_phase5_asset_inventory"
down_revision = "247_merge_phase3_inbox_heads"
branch_labels = None
depends_on = None


def _has_table(name: str) -> bool:
    return name in inspect(op.get_bind()).get_table_names()


def _has_column(table_name: str, column_name: str) -> bool:
    inspector = inspect(op.get_bind())
    if table_name not in inspector.get_table_names():
        return False
    return column_name in {
        column["name"] for column in inspector.get_columns(table_name)
    }


def _uuid_type() -> sa.types.TypeEngine:
    if op.get_bind().dialect.name == "postgresql":
        return postgresql.UUID(as_uuid=True)
    return sa.String(length=36)


def _json_type() -> sa.types.TypeEngine:
    if op.get_bind().dialect.name == "postgresql":
        return postgresql.JSONB(astext_type=sa.Text())
    return sa.JSON()


def upgrade() -> None:
    uuid_type = _uuid_type()
    json_type = _json_type()

    if not _has_table("field_assets"):
        op.create_table(
            "field_assets",
            sa.Column("id", uuid_type, primary_key=True),
            sa.Column("asset_tag", sa.String(length=80), nullable=False),
            sa.Column("asset_type", sa.String(length=40), nullable=False),
            sa.Column("name", sa.String(length=160), nullable=False),
            sa.Column(
                "status",
                sa.String(length=40),
                nullable=False,
                server_default="available",
            ),
            sa.Column("vendor", sa.String(length=120)),
            sa.Column("model", sa.String(length=120)),
            sa.Column("serial_number", sa.String(length=120)),
            sa.Column("registration_number", sa.String(length=80)),
            sa.Column("condition", sa.String(length=80)),
            sa.Column("notes", sa.Text()),
            sa.Column("metadata", json_type),
            sa.Column(
                "is_active",
                sa.Boolean(),
                nullable=False,
                server_default=sa.true(),
            ),
            sa.Column("created_at", sa.DateTime(timezone=True)),
            sa.Column("updated_at", sa.DateTime(timezone=True)),
            sa.UniqueConstraint("asset_tag", name="uq_field_assets_asset_tag"),
            sa.UniqueConstraint("serial_number", name="uq_field_assets_serial_number"),
            sa.CheckConstraint(
                "asset_type IN ('vehicle', 'tool', 'test_equipment', 'mobile_device', "
                "'laptop', 'safety_gear', 'other')",
                name="ck_field_assets_asset_type",
            ),
            sa.CheckConstraint(
                "status IN ('available', 'issued', 'maintenance', 'retired', 'lost')",
                name="ck_field_assets_status",
            ),
        )
        op.create_index(
            "ix_field_assets_type_status",
            "field_assets",
            ["asset_type", "status"],
        )
        op.create_index("ix_field_assets_active", "field_assets", ["is_active"])

    if not _has_table("field_asset_custody"):
        op.create_table(
            "field_asset_custody",
            sa.Column("id", uuid_type, primary_key=True),
            sa.Column("asset_source", sa.String(length=40), nullable=False),
            sa.Column("asset_id", uuid_type, nullable=False),
            sa.Column("field_asset_id", uuid_type),
            sa.Column("technician_id", uuid_type, nullable=False),
            sa.Column("system_user_id", uuid_type),
            sa.Column(
                "status",
                sa.String(length=40),
                nullable=False,
                server_default="issued",
            ),
            sa.Column("issued_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("returned_at", sa.DateTime(timezone=True)),
            sa.Column("condition_on_issue", sa.String(length=80)),
            sa.Column("condition_on_return", sa.String(length=80)),
            sa.Column("notes", sa.Text()),
            sa.Column("metadata", json_type),
            sa.Column("created_at", sa.DateTime(timezone=True)),
            sa.Column("updated_at", sa.DateTime(timezone=True)),
            sa.ForeignKeyConstraint(
                ["field_asset_id"],
                ["field_assets.id"],
                ondelete="CASCADE",
            ),
            sa.ForeignKeyConstraint(["technician_id"], ["technician_profiles.id"]),
            sa.ForeignKeyConstraint(["system_user_id"], ["system_users.id"]),
            sa.CheckConstraint(
                "asset_source IN ('field_inventory', 'field_asset', 'ont', 'cpe', "
                "'olt', 'network_device', 'router')",
                name="ck_field_asset_custody_source",
            ),
            sa.CheckConstraint(
                "status IN ('issued', 'returned', 'lost', 'damaged')",
                name="ck_field_asset_custody_status",
            ),
        )
        op.create_index(
            "ix_field_asset_custody_asset",
            "field_asset_custody",
            ["asset_source", "asset_id"],
        )
        op.create_index(
            "ix_field_asset_custody_technician",
            "field_asset_custody",
            ["technician_id", "status"],
        )
        op.create_index(
            "ix_field_asset_custody_system_user",
            "field_asset_custody",
            ["system_user_id", "status"],
        )
        op.create_index(
            "uq_field_asset_custody_issued_asset",
            "field_asset_custody",
            ["asset_source", "asset_id"],
            unique=True,
            postgresql_where=sa.text("status = 'issued'"),
        )

    if not _has_table("field_erp_sync_events"):
        op.create_table(
            "field_erp_sync_events",
            sa.Column("id", uuid_type, primary_key=True),
            sa.Column("entity_type", sa.String(length=80), nullable=False),
            sa.Column("entity_id", uuid_type, nullable=False),
            sa.Column("action", sa.String(length=80), nullable=False),
            sa.Column(
                "status",
                sa.String(length=40),
                nullable=False,
                server_default="pending",
            ),
            sa.Column("idempotency_key", sa.String(length=180), nullable=False),
            sa.Column("payload", json_type, nullable=False),
            sa.Column("attempts", sa.Integer(), nullable=False, server_default="0"),
            sa.Column("remote_id", sa.String(length=120)),
            sa.Column("remote_number", sa.String(length=120)),
            sa.Column("remote_status", sa.String(length=80)),
            sa.Column("last_error", sa.Text()),
            sa.Column("last_attempt_at", sa.DateTime(timezone=True)),
            sa.Column("synced_at", sa.DateTime(timezone=True)),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
            sa.UniqueConstraint(
                "idempotency_key",
                name="uq_field_erp_sync_events_key",
            ),
            sa.CheckConstraint(
                "status IN ('pending', 'processing', 'synced', 'failed', 'canceled')",
                name="ck_field_erp_sync_events_status",
            ),
        )
        op.create_index(
            "ix_field_erp_sync_events_status",
            "field_erp_sync_events",
            ["status", "created_at"],
        )
        op.create_index(
            "ix_field_erp_sync_events_entity",
            "field_erp_sync_events",
            ["entity_type", "entity_id", "action"],
        )

    if _has_table("field_material_requests"):
        if not _has_column("field_material_requests", "erp_material_request_id"):
            op.add_column(
                "field_material_requests",
                sa.Column("erp_material_request_id", sa.String(length=120)),
            )
        if not _has_column("field_material_requests", "erp_material_status"):
            op.add_column(
                "field_material_requests",
                sa.Column("erp_material_status", sa.String(length=40)),
            )


def downgrade() -> None:
    if _has_table("field_material_requests"):
        if _has_column("field_material_requests", "erp_material_status"):
            op.drop_column("field_material_requests", "erp_material_status")
        if _has_column("field_material_requests", "erp_material_request_id"):
            op.drop_column("field_material_requests", "erp_material_request_id")
    if _has_table("field_erp_sync_events"):
        op.drop_index(
            "ix_field_erp_sync_events_entity",
            table_name="field_erp_sync_events",
        )
        op.drop_index(
            "ix_field_erp_sync_events_status",
            table_name="field_erp_sync_events",
        )
        op.drop_table("field_erp_sync_events")
    if _has_table("field_asset_custody"):
        op.drop_index(
            "uq_field_asset_custody_issued_asset",
            table_name="field_asset_custody",
        )
        op.drop_index(
            "ix_field_asset_custody_system_user", table_name="field_asset_custody"
        )
        op.drop_index(
            "ix_field_asset_custody_technician", table_name="field_asset_custody"
        )
        op.drop_index("ix_field_asset_custody_asset", table_name="field_asset_custody")
        op.drop_table("field_asset_custody")
    if _has_table("field_assets"):
        op.drop_index("ix_field_assets_active", table_name="field_assets")
        op.drop_index("ix_field_assets_type_status", table_name="field_assets")
        op.drop_table("field_assets")
