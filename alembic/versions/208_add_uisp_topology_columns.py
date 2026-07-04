"""Add UISP topology sync columns (relationship layer).

Adds the stable ``uisp_device_id`` upsert key to cpe_devices,
network_devices, olt_devices and ont_units (partial-unique, nullable), the
CPE -> AP edge (``parent_network_device_id``) plus sync bookkeeping columns
on cpe_devices, and the ``wireless_radio`` device type for wireless
customer radios imported from UISP.

Revision ID: 208_add_uisp_topology_columns
Revises: 207_add_cutover_balance_variances
Create Date: 2026-07-04
"""

from __future__ import annotations

import sqlalchemy as sa
from sqlalchemy import inspect
from sqlalchemy.dialects.postgresql import UUID

from alembic import op

revision = "208_add_uisp_topology_columns"
down_revision = "207_add_cutover_balance_variances"
branch_labels = None
depends_on = None

# (table, partial-unique index name) for the shared uisp_device_id key.
_UISP_ID_TABLES = (
    ("cpe_devices", "uq_cpe_devices_uisp_device_id"),
    ("network_devices", "uq_network_devices_uisp_device_id"),
    ("olt_devices", "uq_olt_devices_uisp_device_id"),
    ("ont_units", "uq_ont_units_uisp_device_id"),
)


def _has_column(table: str, column: str) -> bool:
    inspector = inspect(op.get_bind())
    return any(col["name"] == column for col in inspector.get_columns(table))


def _has_index(table: str, index_name: str) -> bool:
    inspector = inspect(op.get_bind())
    return any(index["name"] == index_name for index in inspector.get_indexes(table))


def upgrade() -> None:
    # Wireless customer radios imported from UISP get their own device type.
    op.execute("ALTER TYPE devicetype ADD VALUE IF NOT EXISTS 'wireless_radio'")

    for table, index_name in _UISP_ID_TABLES:
        if not _has_column(table, "uisp_device_id"):
            op.add_column(
                table, sa.Column("uisp_device_id", sa.String(length=64), nullable=True)
            )
        if not _has_index(table, index_name):
            op.create_index(
                index_name,
                table,
                ["uisp_device_id"],
                unique=True,
                postgresql_where=sa.text("uisp_device_id IS NOT NULL"),
            )

    if not _has_column("cpe_devices", "parent_network_device_id"):
        op.add_column(
            "cpe_devices",
            sa.Column(
                "parent_network_device_id",
                UUID(as_uuid=True),
                sa.ForeignKey(
                    "network_devices.id",
                    ondelete="SET NULL",
                    name="fk_cpe_devices_parent_network_device_id",
                ),
                nullable=True,
            ),
        )
    if not _has_column("cpe_devices", "uisp_synced_at"):
        op.add_column(
            "cpe_devices",
            sa.Column("uisp_synced_at", sa.DateTime(timezone=True), nullable=True),
        )
    if not _has_column("cpe_devices", "last_uisp_status"):
        op.add_column(
            "cpe_devices",
            sa.Column("last_uisp_status", sa.String(length=20), nullable=True),
        )


def downgrade() -> None:
    if _has_column("cpe_devices", "last_uisp_status"):
        op.drop_column("cpe_devices", "last_uisp_status")
    if _has_column("cpe_devices", "uisp_synced_at"):
        op.drop_column("cpe_devices", "uisp_synced_at")
    if _has_column("cpe_devices", "parent_network_device_id"):
        op.drop_column("cpe_devices", "parent_network_device_id")

    for table, index_name in _UISP_ID_TABLES:
        if _has_index(table, index_name):
            op.drop_index(index_name, table_name=table)
        if _has_column(table, "uisp_device_id"):
            op.drop_column(table, "uisp_device_id")

    # PostgreSQL does not support removing enum values; 'wireless_radio'
    # stays in the devicetype enum (harmless when unused).
