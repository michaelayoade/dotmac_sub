"""Add VLAN and management IP fields to ont_assignments

Revision ID: 067_add_vlan_and_mgmt_ip_to_assignment
Revises: 066_add_service_config_to_ont_assignment
Create Date: 2026-04-25

Adds:
- internet_vlan_id: FK to vlans table for subscriber internet VLAN
- mgmt_vlan_id: FK to vlans table for management IP VLAN
- mgmt_ip_mode: ENUM (inactive/dhcp/static_ip)
- mgmt_ip_address: Static IP for management (when mgmt_ip_mode=static_ip)
"""

from __future__ import annotations

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision = "067_add_vlan_and_mgmt_ip_to_assignment"
down_revision = "066_add_service_config"
branch_labels = None
depends_on = None


def _column_exists(inspector: sa.Inspector, table: str, column: str) -> bool:
    return any(col["name"] == column for col in inspector.get_columns(table))


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)

    # Add internet_vlan_id FK
    if not _column_exists(inspector, "ont_assignments", "internet_vlan_id"):
        op.add_column(
            "ont_assignments",
            sa.Column(
                "internet_vlan_id",
                postgresql.UUID(as_uuid=True),
                sa.ForeignKey("vlans.id", ondelete="SET NULL"),
                nullable=True,
            ),
        )
        op.create_index(
            "ix_ont_assignments_internet_vlan_id",
            "ont_assignments",
            ["internet_vlan_id"],
        )

    # Add mgmt_vlan_id FK
    if not _column_exists(inspector, "ont_assignments", "mgmt_vlan_id"):
        op.add_column(
            "ont_assignments",
            sa.Column(
                "mgmt_vlan_id",
                postgresql.UUID(as_uuid=True),
                sa.ForeignKey("vlans.id", ondelete="SET NULL"),
                nullable=True,
            ),
        )
        op.create_index(
            "ix_ont_assignments_mgmt_vlan_id",
            "ont_assignments",
            ["mgmt_vlan_id"],
        )

    # Add mgmt_ip_mode enum column
    # The mgmtipmode enum should already exist from migration 066
    if not _column_exists(inspector, "ont_assignments", "mgmt_ip_mode"):
        op.add_column(
            "ont_assignments",
            sa.Column(
                "mgmt_ip_mode",
                postgresql.ENUM(
                    "inactive", "dhcp", "static_ip",
                    name="mgmtipmode",
                    create_type=False,
                ),
                nullable=True,
                server_default="inactive",
            ),
        )

    # Add mgmt_ip_address for static IP assignment
    if not _column_exists(inspector, "ont_assignments", "mgmt_ip_address"):
        op.add_column(
            "ont_assignments",
            sa.Column(
                "mgmt_ip_address",
                sa.String(64),
                nullable=True,
            ),
        )


def downgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)

    if _column_exists(inspector, "ont_assignments", "mgmt_ip_address"):
        op.drop_column("ont_assignments", "mgmt_ip_address")

    if _column_exists(inspector, "ont_assignments", "mgmt_ip_mode"):
        op.drop_column("ont_assignments", "mgmt_ip_mode")

    if _column_exists(inspector, "ont_assignments", "mgmt_vlan_id"):
        op.drop_index("ix_ont_assignments_mgmt_vlan_id", table_name="ont_assignments")
        op.drop_column("ont_assignments", "mgmt_vlan_id")

    if _column_exists(inspector, "ont_assignments", "internet_vlan_id"):
        op.drop_index(
            "ix_ont_assignments_internet_vlan_id", table_name="ont_assignments"
        )
        op.drop_column("ont_assignments", "internet_vlan_id")
