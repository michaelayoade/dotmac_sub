"""Topology reconcile: Zabbix linkage columns on network_devices + pop_sites.

Phase 1 of the automatic network-topology / customer-path feature. The reconcile
(app/services/topology/zabbix_reconcile.py) merges Zabbix structure onto the
EXISTING tables rather than new ones:

- network_devices gains the stable Zabbix key (zabbix_hostid), provenance
  (source, last_synced_at, role_source) and the matched provisioning device
  (matched_device_type/matched_device_id).
- pop_sites gains zabbix_group_id so a pop_site can be matched to a Zabbix
  "X BTS" host group (and survive BTS renames).

Both keys are partial-unique (WHERE NOT NULL) so the many pre-existing rows that
are not yet linked to Zabbix (orphaned Splynx imports; non-BTS / region
pop_sites) stay NULL and unconstrained.

Revision ID: 153_topology_zabbix_linkage
Revises: 152_subscriber_additional_routes
Create Date: 2026-06-16
"""

from __future__ import annotations

import sqlalchemy as sa
from sqlalchemy import inspect
from sqlalchemy.dialects.postgresql import UUID

from alembic import op

revision = "153_topology_zabbix_linkage"
down_revision = "152_subscriber_additional_routes"
branch_labels = None
depends_on = None


def _columns(table: str) -> set[str]:
    return {c["name"] for c in inspect(op.get_bind()).get_columns(table)}


def _indexes(table: str) -> set[str]:
    return {i["name"] for i in inspect(op.get_bind()).get_indexes(table)}


def _is_postgres() -> bool:
    return op.get_bind().dialect.name == "postgresql"


def upgrade() -> None:
    nd_cols = _columns("network_devices")
    nd_adds = [
        ("zabbix_hostid", sa.Column("zabbix_hostid", sa.String(20), nullable=True)),
        ("source", sa.Column("source", sa.String(40), nullable=True)),
        (
            "last_synced_at",
            sa.Column("last_synced_at", sa.DateTime(timezone=True), nullable=True),
        ),
        ("role_source", sa.Column("role_source", sa.String(20), nullable=True)),
        (
            "matched_device_type",
            sa.Column("matched_device_type", sa.String(20), nullable=True),
        ),
        (
            "matched_device_id",
            sa.Column("matched_device_id", UUID(as_uuid=True), nullable=True),
        ),
    ]
    for name, col in nd_adds:
        if name not in nd_cols:
            op.add_column("network_devices", col)

    if "zabbix_group_id" not in _columns("pop_sites"):
        op.add_column(
            "pop_sites", sa.Column("zabbix_group_id", sa.String(20), nullable=True)
        )

    # Partial-unique indexes (WHERE NOT NULL). On PostgreSQL these are partial;
    # on other dialects fall back to a plain non-unique index (tests build the
    # schema from the models, not from this migration).
    nd_idx = _indexes("network_devices")
    if "uq_network_devices_zabbix_hostid" not in nd_idx:
        op.create_index(
            "uq_network_devices_zabbix_hostid",
            "network_devices",
            ["zabbix_hostid"],
            unique=_is_postgres(),
            postgresql_where=sa.text("zabbix_hostid IS NOT NULL"),
        )
    ps_idx = _indexes("pop_sites")
    if "uq_pop_sites_zabbix_group_id" not in ps_idx:
        op.create_index(
            "uq_pop_sites_zabbix_group_id",
            "pop_sites",
            ["zabbix_group_id"],
            unique=_is_postgres(),
            postgresql_where=sa.text("zabbix_group_id IS NOT NULL"),
        )


def downgrade() -> None:
    if "uq_pop_sites_zabbix_group_id" in _indexes("pop_sites"):
        op.drop_index("uq_pop_sites_zabbix_group_id", table_name="pop_sites")
    if "uq_network_devices_zabbix_hostid" in _indexes("network_devices"):
        op.drop_index("uq_network_devices_zabbix_hostid", table_name="network_devices")

    if "zabbix_group_id" in _columns("pop_sites"):
        op.drop_column("pop_sites", "zabbix_group_id")
    nd_cols = _columns("network_devices")
    for name in (
        "matched_device_id",
        "matched_device_type",
        "role_source",
        "last_synced_at",
        "source",
        "zabbix_hostid",
    ):
        if name in nd_cols:
            op.drop_column("network_devices", name)
