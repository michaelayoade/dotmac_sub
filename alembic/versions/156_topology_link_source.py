"""Topology Phase 2: network_topology_links provenance (source + last_seen_at).

The LLDP neighbor poller (app/services/topology/lldp_poller.py) owns rows with
source='lldp_neighbor' — upserting + soft-pruning only those, never touching
manual/other-sourced links. last_seen_at bumps each poll an edge is observed.

Revision ID: 156_topology_link_source
Revises: 155_topology_live_status
Create Date: 2026-06-17
"""

from __future__ import annotations

import sqlalchemy as sa
from sqlalchemy import inspect

from alembic import op

revision = "156_topology_link_source"
down_revision = "155_topology_live_status"
branch_labels = None
depends_on = None


def _columns(table: str) -> set[str]:
    return {c["name"] for c in inspect(op.get_bind()).get_columns(table)}


def upgrade() -> None:
    cols = _columns("network_topology_links")
    if "source" not in cols:
        op.add_column(
            "network_topology_links", sa.Column("source", sa.String(40), nullable=True)
        )
    if "last_seen_at" not in cols:
        op.add_column(
            "network_topology_links",
            sa.Column("last_seen_at", sa.DateTime(timezone=True), nullable=True),
        )


def downgrade() -> None:
    cols = _columns("network_topology_links")
    if "last_seen_at" in cols:
        op.drop_column("network_topology_links", "last_seen_at")
    if "source" in cols:
        op.drop_column("network_topology_links", "source")
