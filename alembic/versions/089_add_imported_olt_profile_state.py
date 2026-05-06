"""Add imported OLT profile state tables.

Revision ID: 089_add_imported_olt_profile_state
Revises: 088_add_olt_wan_provisioning_mode
Create Date: 2026-05-06
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "089_add_imported_olt_profile_state"
down_revision = "088_add_olt_wan_provisioning_mode"
branch_labels = None
depends_on = None


def _table_exists(table_name: str) -> bool:
    inspector = sa.inspect(op.get_bind())
    return table_name in inspector.get_table_names()


def upgrade() -> None:
    if not _table_exists("olt_line_profiles"):
        op.create_table(
            "olt_line_profiles",
            sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
            sa.Column("olt_id", postgresql.UUID(as_uuid=True), nullable=False),
            sa.Column("profile_id", sa.Integer(), nullable=False),
            sa.Column("name", sa.String(length=160), nullable=True),
            sa.Column("binding_count", sa.Integer(), nullable=False, server_default="0"),
            sa.Column("tr069_management_enabled", sa.Boolean(), nullable=True),
            sa.Column("raw_config", sa.Text(), nullable=True),
            sa.Column("last_imported_at", sa.DateTime(timezone=True), nullable=False),
            sa.ForeignKeyConstraint(
                ["olt_id"],
                ["olt_devices.id"],
                ondelete="CASCADE",
            ),
            sa.PrimaryKeyConstraint("id"),
            sa.UniqueConstraint(
                "olt_id",
                "profile_id",
                name="uq_olt_line_profiles_olt_profile",
            ),
        )

    if not _table_exists("olt_service_profiles"):
        op.create_table(
            "olt_service_profiles",
            sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
            sa.Column("olt_id", postgresql.UUID(as_uuid=True), nullable=False),
            sa.Column("profile_id", sa.Integer(), nullable=False),
            sa.Column("name", sa.String(length=160), nullable=True),
            sa.Column("binding_count", sa.Integer(), nullable=False, server_default="0"),
            sa.Column("ethernet_ports", sa.Integer(), nullable=True),
            sa.Column("voip_ports", sa.Integer(), nullable=True),
            sa.Column("catv_ports", sa.Integer(), nullable=True),
            sa.Column("raw_config", sa.Text(), nullable=True),
            sa.Column("last_imported_at", sa.DateTime(timezone=True), nullable=False),
            sa.ForeignKeyConstraint(
                ["olt_id"],
                ["olt_devices.id"],
                ondelete="CASCADE",
            ),
            sa.PrimaryKeyConstraint("id"),
            sa.UniqueConstraint(
                "olt_id",
                "profile_id",
                name="uq_olt_service_profiles_olt_profile",
            ),
        )

    if not _table_exists("olt_ont_registrations"):
        op.create_table(
            "olt_ont_registrations",
            sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
            sa.Column("olt_id", postgresql.UUID(as_uuid=True), nullable=False),
            sa.Column("fsp", sa.String(length=32), nullable=False),
            sa.Column("ont_id_on_olt", sa.Integer(), nullable=False),
            sa.Column("serial_number", sa.String(length=120), nullable=True),
            sa.Column("equipment_id", sa.String(length=120), nullable=True),
            sa.Column("line_profile_id", sa.Integer(), nullable=True),
            sa.Column("service_profile_id", sa.Integer(), nullable=True),
            sa.Column("tr069_profile_id", sa.Integer(), nullable=True),
            sa.Column("match_state", sa.String(length=40), nullable=True),
            sa.Column("description", sa.Text(), nullable=True),
            sa.Column("raw_config", sa.Text(), nullable=True),
            sa.Column("is_active", sa.Boolean(), nullable=False, server_default="true"),
            sa.Column("last_imported_at", sa.DateTime(timezone=True), nullable=False),
            sa.ForeignKeyConstraint(
                ["olt_id"],
                ["olt_devices.id"],
                ondelete="CASCADE",
            ),
            sa.ForeignKeyConstraint(
                ["olt_id", "line_profile_id"],
                ["olt_line_profiles.olt_id", "olt_line_profiles.profile_id"],
                name="fk_olt_ont_registration_line_profile",
            ),
            sa.ForeignKeyConstraint(
                ["olt_id", "service_profile_id"],
                ["olt_service_profiles.olt_id", "olt_service_profiles.profile_id"],
                name="fk_olt_ont_registration_service_profile",
            ),
            sa.PrimaryKeyConstraint("id"),
            sa.UniqueConstraint(
                "olt_id",
                "fsp",
                "ont_id_on_olt",
                name="uq_olt_ont_registrations_olt_fsp_ont",
            ),
        )
        op.create_index(
            "uq_olt_ont_registrations_active_serial",
            "olt_ont_registrations",
            ["olt_id", "serial_number"],
            unique=True,
            postgresql_where=sa.text("is_active = true"),
        )

    if not _table_exists("olt_onu_type_profile_mappings"):
        op.create_table(
            "olt_onu_type_profile_mappings",
            sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
            sa.Column("olt_id", postgresql.UUID(as_uuid=True), nullable=False),
            sa.Column("equipment_id", sa.String(length=120), nullable=False),
            sa.Column("onu_type_id", postgresql.UUID(as_uuid=True), nullable=True),
            sa.Column("line_profile_id", sa.Integer(), nullable=False),
            sa.Column("service_profile_id", sa.Integer(), nullable=False),
            sa.Column(
                "source_registration_count",
                sa.Integer(),
                nullable=False,
                server_default="0",
            ),
            sa.Column("last_imported_at", sa.DateTime(timezone=True), nullable=False),
            sa.ForeignKeyConstraint(
                ["olt_id"],
                ["olt_devices.id"],
                ondelete="CASCADE",
            ),
            sa.ForeignKeyConstraint(
                ["onu_type_id"],
                ["onu_types.id"],
                ondelete="SET NULL",
            ),
            sa.ForeignKeyConstraint(
                ["olt_id", "line_profile_id"],
                ["olt_line_profiles.olt_id", "olt_line_profiles.profile_id"],
                name="fk_olt_onu_mapping_line_profile",
            ),
            sa.ForeignKeyConstraint(
                ["olt_id", "service_profile_id"],
                ["olt_service_profiles.olt_id", "olt_service_profiles.profile_id"],
                name="fk_olt_onu_mapping_service_profile",
            ),
            sa.PrimaryKeyConstraint("id"),
            sa.UniqueConstraint(
                "olt_id",
                "equipment_id",
                name="uq_olt_onu_type_profile_mappings_olt_equipment",
            ),
        )


def downgrade() -> None:
    if _table_exists("olt_onu_type_profile_mappings"):
        op.drop_table("olt_onu_type_profile_mappings")
    if _table_exists("olt_ont_registrations"):
        op.drop_index(
            "uq_olt_ont_registrations_active_serial",
            table_name="olt_ont_registrations",
        )
        op.drop_table("olt_ont_registrations")
    if _table_exists("olt_service_profiles"):
        op.drop_table("olt_service_profiles")
    if _table_exists("olt_line_profiles"):
        op.drop_table("olt_line_profiles")
