"""Add persisted OLT profile bundles.

Revision ID: 098_add_olt_profile_bundles
Revises: 097_add_wan_behavior_to_olt_onu_type_mappings
Create Date: 2026-05-08
"""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision = "098_add_olt_profile_bundles"
down_revision = "097_add_wan_behavior_to_olt_onu_type_mappings"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "catalog_offers",
        sa.Column(
            "olt_profile_auto_sync_enabled",
            sa.Boolean(),
            nullable=False,
            server_default=sa.false(),
        ),
    )
    op.create_table(
        "olt_profile_bundles",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("olt_id", sa.UUID(), nullable=False),
        sa.Column("offer_id", sa.UUID(), nullable=False),
        sa.Column("name", sa.String(length=160), nullable=False),
        sa.Column("checksum", sa.String(length=64), nullable=False),
        sa.Column("vlan_id", sa.Integer(), nullable=False),
        sa.Column("download_kbps", sa.Integer(), nullable=False),
        sa.Column("upload_kbps", sa.Integer(), nullable=False),
        sa.Column("dba_profile_id", sa.Integer(), nullable=False),
        sa.Column("download_traffic_table_id", sa.Integer(), nullable=False),
        sa.Column("upload_traffic_table_id", sa.Integer(), nullable=False),
        sa.Column("line_profile_id", sa.Integer(), nullable=False),
        sa.Column("service_profile_id", sa.Integer(), nullable=False),
        sa.Column("wan_profile_id", sa.Integer(), nullable=True),
        sa.Column("tr069_profile_id", sa.Integer(), nullable=True),
        sa.Column("gem_id", sa.Integer(), nullable=False),
        sa.Column("tcont_id", sa.Integer(), nullable=False),
        sa.Column("command_plan", sa.JSON(), nullable=True),
        sa.Column("drift_status", sa.String(length=40), nullable=True),
        sa.Column("drift_details", sa.JSON(), nullable=True),
        sa.Column("last_applied_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_verified_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("is_active", sa.Boolean(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["offer_id"], ["catalog_offers.id"], ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(["olt_id"], ["olt_devices.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "olt_id",
            "offer_id",
            name="uq_olt_profile_bundles_olt_offer",
        ),
    )
    op.create_index(
        "ix_olt_profile_bundles_olt_active",
        "olt_profile_bundles",
        ["olt_id", "is_active"],
    )
    op.create_index(
        "ix_olt_profile_bundles_checksum",
        "olt_profile_bundles",
        ["checksum"],
    )
    op.create_table(
        "olt_profile_sync_tasks",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("olt_id", sa.UUID(), nullable=False),
        sa.Column("offer_id", sa.UUID(), nullable=False),
        sa.Column("status", sa.String(length=40), nullable=False),
        sa.Column("trigger", sa.String(length=80), nullable=False),
        sa.Column("requested_by", sa.String(length=120), nullable=True),
        sa.Column("approved_by", sa.String(length=120), nullable=True),
        sa.Column("approved_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("scheduled_for", sa.DateTime(timezone=True), nullable=True),
        sa.Column("preview_payload", sa.JSON(), nullable=True),
        sa.Column("result_payload", sa.JSON(), nullable=True),
        sa.Column("error", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["offer_id"], ["catalog_offers.id"], ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(["olt_id"], ["olt_devices.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_olt_profile_sync_tasks_status",
        "olt_profile_sync_tasks",
        ["status"],
    )
    op.create_index(
        "ix_olt_profile_sync_tasks_olt_offer_status",
        "olt_profile_sync_tasks",
        ["olt_id", "offer_id", "status"],
    )


def downgrade() -> None:
    op.drop_index(
        "ix_olt_profile_sync_tasks_olt_offer_status",
        table_name="olt_profile_sync_tasks",
    )
    op.drop_index(
        "ix_olt_profile_sync_tasks_status",
        table_name="olt_profile_sync_tasks",
    )
    op.drop_table("olt_profile_sync_tasks")
    op.drop_index(
        "ix_olt_profile_bundles_checksum",
        table_name="olt_profile_bundles",
    )
    op.drop_index(
        "ix_olt_profile_bundles_olt_active",
        table_name="olt_profile_bundles",
    )
    op.drop_table("olt_profile_bundles")
    op.drop_column("catalog_offers", "olt_profile_auto_sync_enabled")
