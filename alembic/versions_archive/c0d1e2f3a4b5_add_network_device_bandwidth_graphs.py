"""Add network device bandwidth graph tables.

Revision ID: c0d1e2f3a4b5
Revises: b9c0d1e2f3a4
Create Date: 2026-02-25
"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy import inspect
from sqlalchemy.dialects.postgresql import UUID

from alembic import op

revision: str = "c0d1e2f3a4b5"
down_revision: str | Sequence[str] | None = "b9c0d1e2f3a4"
branch_labels: Sequence[str] | None = None
depends_on: Sequence[str] | None = None


def upgrade() -> None:
    bind = op.get_bind()
    inspector = inspect(bind)

    if not inspector.has_table("network_device_bandwidth_graphs"):
        op.create_table(
            "network_device_bandwidth_graphs",
            sa.Column("id", UUID(as_uuid=True), primary_key=True),
            sa.Column("device_id", UUID(as_uuid=True), sa.ForeignKey("network_devices.id"), nullable=False),
            sa.Column("title", sa.String(length=200), nullable=False),
            sa.Column("vertical_axis_title", sa.String(length=80), nullable=False, server_default="Bandwidth"),
            sa.Column("height_px", sa.Integer(), nullable=False, server_default=sa.text("150")),
            sa.Column("is_public", sa.Boolean(), nullable=False, server_default=sa.text("false")),
            sa.Column("public_token", sa.String(length=64), nullable=True),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
            sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
        )
        op.create_index(
            "ix_network_device_bandwidth_graphs_device_id",
            "network_device_bandwidth_graphs",
            ["device_id"],
            unique=False,
        )
        op.create_index(
            "ux_network_device_bandwidth_graphs_public_token",
            "network_device_bandwidth_graphs",
            ["public_token"],
            unique=True,
        )

    inspector = inspect(bind)
    if not inspector.has_table("network_device_bandwidth_graph_sources"):
        op.create_table(
            "network_device_bandwidth_graph_sources",
            sa.Column("id", UUID(as_uuid=True), primary_key=True),
            sa.Column(
                "graph_id",
                UUID(as_uuid=True),
                sa.ForeignKey("network_device_bandwidth_graphs.id", ondelete="CASCADE"),
                nullable=False,
            ),
            sa.Column("source_device_id", UUID(as_uuid=True), sa.ForeignKey("network_devices.id"), nullable=False),
            sa.Column("snmp_oid_id", UUID(as_uuid=True), sa.ForeignKey("network_device_snmp_oids.id"), nullable=False),
            sa.Column("factor", sa.Float(), nullable=False, server_default=sa.text("1.0")),
            sa.Column("color_hex", sa.String(length=7), nullable=False, server_default="#22c55e"),
            sa.Column("draw_type", sa.String(length=16), nullable=False, server_default="LINE1"),
            sa.Column("stack_enabled", sa.Boolean(), nullable=False, server_default=sa.text("false")),
            sa.Column("value_unit", sa.String(length=12), nullable=False, server_default="Bps"),
            sa.Column("sort_order", sa.Integer(), nullable=False, server_default=sa.text("0")),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
            sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
        )
        op.create_index(
            "ix_network_device_bandwidth_graph_sources_graph_id",
            "network_device_bandwidth_graph_sources",
            ["graph_id"],
            unique=False,
        )


def downgrade() -> None:
    bind = op.get_bind()
    inspector = inspect(bind)

    if inspector.has_table("network_device_bandwidth_graph_sources"):
        op.drop_index(
            "ix_network_device_bandwidth_graph_sources_graph_id",
            table_name="network_device_bandwidth_graph_sources",
        )
        op.drop_table("network_device_bandwidth_graph_sources")

    inspector = inspect(bind)
    if inspector.has_table("network_device_bandwidth_graphs"):
        op.drop_index(
            "ux_network_device_bandwidth_graphs_public_token",
            table_name="network_device_bandwidth_graphs",
        )
        op.drop_index(
            "ix_network_device_bandwidth_graphs_device_id",
            table_name="network_device_bandwidth_graphs",
        )
        op.drop_table("network_device_bandwidth_graphs")
