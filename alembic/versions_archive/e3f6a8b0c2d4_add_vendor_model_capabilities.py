"""Add vendor model capability and TR-069 parameter map tables.

Revision ID: e3f6a8b0c2d4
Revises: d2e5f7a9b1c3
Create Date: 2026-03-10
"""

import sqlalchemy as sa
from sqlalchemy import inspect
from sqlalchemy.dialects.postgresql import UUID

from alembic import op

revision = "e3f6a8b0c2d4"
down_revision = "d2e5f7a9b1c3"
branch_labels = None
depends_on = None


def upgrade() -> None:
    conn = op.get_bind()
    inspector = inspect(conn)

    # Create vendor_model_capabilities table
    if not inspector.has_table("vendor_model_capabilities"):
        op.create_table(
            "vendor_model_capabilities",
            sa.Column("id", UUID(as_uuid=True), primary_key=True),
            sa.Column("vendor", sa.String(120), nullable=False),
            sa.Column("model", sa.String(120), nullable=False),
            sa.Column("firmware_pattern", sa.String(200)),
            sa.Column("tr069_root", sa.String(200)),
            sa.Column("supported_features", sa.JSON),
            sa.Column("max_wan_services", sa.Integer, server_default="1"),
            sa.Column("max_lan_ports", sa.Integer, server_default="4"),
            sa.Column("max_ssids", sa.Integer, server_default="2"),
            sa.Column("supports_vlan_tagging", sa.Boolean, server_default="true"),
            sa.Column("supports_qinq", sa.Boolean, server_default="false"),
            sa.Column("supports_ipv6", sa.Boolean, server_default="false"),
            sa.Column("is_active", sa.Boolean, server_default="true"),
            sa.Column("notes", sa.Text),
            sa.Column(
                "created_at",
                sa.DateTime(timezone=True),
                server_default=sa.func.now(),
            ),
            sa.Column(
                "updated_at",
                sa.DateTime(timezone=True),
                server_default=sa.func.now(),
            ),
            sa.UniqueConstraint(
                "vendor",
                "model",
                "firmware_pattern",
                name="uq_vmc_vendor_model_fw",
            ),
        )

    # Create tr069_parameter_maps table
    if not inspector.has_table("tr069_parameter_maps"):
        op.create_table(
            "tr069_parameter_maps",
            sa.Column("id", UUID(as_uuid=True), primary_key=True),
            sa.Column(
                "capability_id",
                UUID(as_uuid=True),
                sa.ForeignKey("vendor_model_capabilities.id", ondelete="CASCADE"),
                nullable=False,
            ),
            sa.Column("canonical_name", sa.String(200), nullable=False),
            sa.Column("tr069_path", sa.String(500), nullable=False),
            sa.Column("writable", sa.Boolean, server_default="true"),
            sa.Column("value_type", sa.String(40)),
            sa.Column("notes", sa.Text),
            sa.Column(
                "created_at",
                sa.DateTime(timezone=True),
                server_default=sa.func.now(),
            ),
            sa.Column(
                "updated_at",
                sa.DateTime(timezone=True),
                server_default=sa.func.now(),
            ),
            sa.UniqueConstraint(
                "capability_id",
                "canonical_name",
                name="uq_tr069_param_cap_canonical",
            ),
        )


def downgrade() -> None:
    op.drop_table("tr069_parameter_maps")
    op.drop_table("vendor_model_capabilities")
