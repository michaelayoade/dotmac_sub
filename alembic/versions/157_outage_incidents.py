"""Topology Phase 4b: outage_incidents (operator-declared outages).

Manual outage management — a row per declared outage against a node/basestation,
with a snapshotted affected_count. No auto-detection.

Revision ID: 157_outage_incidents
Revises: 156_topology_link_source
Create Date: 2026-06-17
"""

from __future__ import annotations

import sqlalchemy as sa
from sqlalchemy import inspect
from sqlalchemy.dialects.postgresql import UUID

from alembic import op

revision = "157_outage_incidents"
down_revision = "156_topology_link_source"
branch_labels = None
depends_on = None

TABLE = "outage_incidents"


def _has_table(name: str) -> bool:
    return name in inspect(op.get_bind()).get_table_names()


def upgrade() -> None:
    if _has_table(TABLE):
        return
    op.create_table(
        TABLE,
        sa.Column("id", UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "root_node_id", UUID(as_uuid=True), sa.ForeignKey("network_devices.id")
        ),
        sa.Column("basestation_id", UUID(as_uuid=True), sa.ForeignKey("pop_sites.id")),
        sa.Column("declared_by", sa.String(120)),
        sa.Column("status", sa.String(20), nullable=False, server_default="open"),
        sa.Column("severity", sa.String(20)),
        sa.Column("affected_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("note", sa.Text()),
        sa.Column(
            "started_at", sa.DateTime(timezone=True), server_default=sa.func.now()
        ),
        sa.Column("resolved_at", sa.DateTime(timezone=True)),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now()
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=sa.func.now()
        ),
    )
    op.create_index("ix_outage_incidents_status", TABLE, ["status"])
    op.create_index("ix_outage_incidents_root_node", TABLE, ["root_node_id"])
    op.create_index("ix_outage_incidents_basestation", TABLE, ["basestation_id"])


def downgrade() -> None:
    if not _has_table(TABLE):
        return
    op.drop_index("ix_outage_incidents_basestation", table_name=TABLE)
    op.drop_index("ix_outage_incidents_root_node", table_name=TABLE)
    op.drop_index("ix_outage_incidents_status", table_name=TABLE)
    op.drop_table(TABLE)
