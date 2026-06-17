"""Topology Phase 3: live_status cache columns on network_devices.

Warmed from Zabbix (host availability + active triggers) by a background task
and read by the Network Path panel — never fetched on the request path. Kept
separate from the ping/snmp ``status`` column (different writer).

Revision ID: 155_topology_live_status
Revises: 154_relax_inactive_olt_config_pack_constraint
Create Date: 2026-06-17

Re-parented onto 154_relax (an orphaned-but-prod-applied migration vendored into
the tree in this PR) instead of 153, so the chain is linear
153 -> 154_relax -> 155 and prod (already at 154_relax) upgrades cleanly.
"""

from __future__ import annotations

import sqlalchemy as sa
from sqlalchemy import inspect

from alembic import op

revision = "155_topology_live_status"
down_revision = "154_relax_inactive_olt_config_pack_constraint"
branch_labels = None
depends_on = None


def _columns(table: str) -> set[str]:
    return {c["name"] for c in inspect(op.get_bind()).get_columns(table)}


def upgrade() -> None:
    cols = _columns("network_devices")
    if "live_status" not in cols:
        op.add_column(
            "network_devices", sa.Column("live_status", sa.String(20), nullable=True)
        )
    if "live_status_at" not in cols:
        op.add_column(
            "network_devices",
            sa.Column("live_status_at", sa.DateTime(timezone=True), nullable=True),
        )


def downgrade() -> None:
    cols = _columns("network_devices")
    if "live_status_at" in cols:
        op.drop_column("network_devices", "live_status_at")
    if "live_status" in cols:
        op.drop_column("network_devices", "live_status")
