"""Add field location tracking tables.

Revision ID: 226_field_location_tracking
Revises: 225_field_device_tokens
Create Date: 2026-07-09
"""

from __future__ import annotations

import sqlalchemy as sa
from sqlalchemy import inspect
from sqlalchemy.dialects import postgresql

from alembic import op

revision = "226_field_location_tracking"
down_revision = "225_field_device_tokens"
branch_labels = None
depends_on = None


def _has_table(name: str) -> bool:
    return name in inspect(op.get_bind()).get_table_names()


def upgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name == "sqlite":
        return
    if not _has_table("field_tech_presence"):
        op.create_table(
            "field_tech_presence",
            sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
            sa.Column(
                "technician_id",
                postgresql.UUID(as_uuid=True),
                sa.ForeignKey("technician_profiles.id"),
                nullable=False,
            ),
            sa.Column("person_id", postgresql.UUID(as_uuid=True), nullable=False),
            sa.Column("status", sa.String(length=20), nullable=False),
            sa.Column("location_sharing_enabled", sa.Boolean(), nullable=False),
            sa.Column("last_latitude", sa.Float()),
            sa.Column("last_longitude", sa.Float()),
            sa.Column("last_location_accuracy_m", sa.Float()),
            sa.Column("last_location_at", sa.DateTime(timezone=True)),
            sa.Column("last_seen_at", sa.DateTime(timezone=True)),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
            sa.CheckConstraint(
                "status IN ('off_shift', 'on_shift', 'break', 'busy')",
                name="ck_field_tech_presence_status",
            ),
        )
        op.create_index(
            "ix_field_tech_presence_technician_id",
            "field_tech_presence",
            ["technician_id"],
            unique=True,
        )
        op.create_index(
            "ix_field_tech_presence_person_id",
            "field_tech_presence",
            ["person_id"],
        )
        op.create_index(
            "ix_field_tech_presence_status", "field_tech_presence", ["status"]
        )
        op.create_index(
            "ix_field_tech_presence_last_location_at",
            "field_tech_presence",
            ["last_location_at"],
        )
    if not _has_table("field_tech_location_pings"):
        op.create_table(
            "field_tech_location_pings",
            sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
            sa.Column(
                "technician_id",
                postgresql.UUID(as_uuid=True),
                sa.ForeignKey("technician_profiles.id"),
                nullable=False,
            ),
            sa.Column("person_id", postgresql.UUID(as_uuid=True), nullable=False),
            sa.Column("crm_work_order_id", sa.String(length=64)),
            sa.Column("latitude", sa.Float(), nullable=False),
            sa.Column("longitude", sa.Float(), nullable=False),
            sa.Column("accuracy_m", sa.Float()),
            sa.Column("captured_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("received_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("source", sa.String(length=32), nullable=False),
            sa.CheckConstraint(
                "latitude >= -90 AND latitude <= 90",
                name="ck_field_tech_location_pings_lat_range",
            ),
            sa.CheckConstraint(
                "longitude >= -180 AND longitude <= 180",
                name="ck_field_tech_location_pings_lng_range",
            ),
        )
        op.create_index(
            "ix_field_tech_location_pings_technician_received",
            "field_tech_location_pings",
            ["technician_id", "received_at"],
        )
        op.create_index(
            "ix_field_tech_location_pings_person_received",
            "field_tech_location_pings",
            ["person_id", "received_at"],
        )
        op.create_index(
            "ix_field_tech_location_pings_received_at",
            "field_tech_location_pings",
            ["received_at"],
        )
        op.create_index(
            "ix_field_tech_location_pings_crm_work_order_id",
            "field_tech_location_pings",
            ["crm_work_order_id"],
        )


def downgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name == "sqlite":
        return
    if _has_table("field_tech_location_pings"):
        op.drop_index(
            "ix_field_tech_location_pings_crm_work_order_id",
            table_name="field_tech_location_pings",
        )
        op.drop_index(
            "ix_field_tech_location_pings_received_at",
            table_name="field_tech_location_pings",
        )
        op.drop_index(
            "ix_field_tech_location_pings_person_received",
            table_name="field_tech_location_pings",
        )
        op.drop_index(
            "ix_field_tech_location_pings_technician_received",
            table_name="field_tech_location_pings",
        )
        op.drop_table("field_tech_location_pings")
    if _has_table("field_tech_presence"):
        op.drop_index(
            "ix_field_tech_presence_last_location_at",
            table_name="field_tech_presence",
        )
        op.drop_index("ix_field_tech_presence_status", table_name="field_tech_presence")
        op.drop_index(
            "ix_field_tech_presence_person_id", table_name="field_tech_presence"
        )
        op.drop_index(
            "ix_field_tech_presence_technician_id", table_name="field_tech_presence"
        )
        op.drop_table("field_tech_presence")
