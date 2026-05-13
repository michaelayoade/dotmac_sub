"""Add imported OLT service-port state.

Revision ID: 096_add_imported_olt_service_ports
Revises: 095_drop_ont_provisioning_profile_auth_defaults
Create Date: 2026-05-06
"""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision = "096_add_imported_olt_service_ports"
down_revision = "095_drop_ont_provisioning_profile_auth_defaults"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "olt_service_ports",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("olt_device_id", sa.UUID(), nullable=False),
        sa.Column("ont_unit_id", sa.UUID(), nullable=True),
        sa.Column("port_index", sa.Integer(), nullable=False),
        sa.Column("fsp", sa.String(length=32), nullable=False),
        sa.Column("ont_id_on_olt", sa.Integer(), nullable=False),
        sa.Column("vlan_id", sa.Integer(), nullable=False),
        sa.Column("gem_index", sa.Integer(), nullable=False),
        sa.Column("user_vlan", sa.String(length=32), nullable=True),
        sa.Column("tag_transform", sa.String(length=40), nullable=True),
        sa.Column("flow_type", sa.String(length=40), nullable=True),
        sa.Column("flow_para", sa.String(length=64), nullable=True),
        sa.Column("state", sa.String(length=32), nullable=True),
        sa.Column("source", sa.String(length=40), nullable=False),
        sa.Column("raw_entry", sa.JSON(), nullable=True),
        sa.Column("last_imported_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["olt_device_id"],
            ["olt_devices.id"],
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["ont_unit_id"],
            ["ont_units.id"],
            ondelete="SET NULL",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "olt_device_id",
            "port_index",
            name="uq_olt_service_ports_olt_port_index",
        ),
    )
    op.create_index(
        "ix_olt_service_ports_olt_fsp",
        "olt_service_ports",
        ["olt_device_id", "fsp"],
    )
    op.create_index("ix_olt_service_ports_ont", "olt_service_ports", ["ont_unit_id"])
    op.create_index(
        "ix_olt_service_ports_vlan_gem",
        "olt_service_ports",
        ["vlan_id", "gem_index"],
    )


def downgrade() -> None:
    op.drop_index("ix_olt_service_ports_vlan_gem", table_name="olt_service_ports")
    op.drop_index("ix_olt_service_ports_ont", table_name="olt_service_ports")
    op.drop_index("ix_olt_service_ports_olt_fsp", table_name="olt_service_ports")
    op.drop_table("olt_service_ports")
