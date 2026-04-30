"""Add ONT management IP tracking to IPv4 addresses.

Revision ID: 080_add_ont_management_ip_tracking
Revises: 079_add_warning_network_operation_status
Create Date: 2026-04-30
"""

from __future__ import annotations

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision = "080_add_ont_management_ip_tracking"
down_revision = "079_add_warning_network_operation_status"
branch_labels = None
depends_on = None


def _column_exists(inspector: sa.Inspector, table: str, column: str) -> bool:
    return any(col["name"] == column for col in inspector.get_columns(table))


def _index_exists(inspector: sa.Inspector, table: str, index_name: str) -> bool:
    return any(index["name"] == index_name for index in inspector.get_indexes(table))


def _fk_exists(inspector: sa.Inspector, table: str, fk_name: str) -> bool:
    return any(fk["name"] == fk_name for fk in inspector.get_foreign_keys(table))


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)

    if not _column_exists(inspector, "ipv4_addresses", "ont_unit_id"):
        op.add_column(
            "ipv4_addresses",
            sa.Column("ont_unit_id", postgresql.UUID(as_uuid=True), nullable=True),
        )

    if not _fk_exists(
        inspector, "ipv4_addresses", "fk_ipv4_addresses_ont_unit_id_ont_units"
    ):
        op.create_foreign_key(
            "fk_ipv4_addresses_ont_unit_id_ont_units",
            "ipv4_addresses",
            "ont_units",
            ["ont_unit_id"],
            ["id"],
            ondelete="SET NULL",
        )

    if not _index_exists(inspector, "ipv4_addresses", "ix_ipv4_addresses_ont_unit_id"):
        op.create_index(
            "ix_ipv4_addresses_ont_unit_id",
            "ipv4_addresses",
            ["ont_unit_id"],
        )

    if not _column_exists(inspector, "ipv4_addresses", "allocation_type"):
        op.add_column(
            "ipv4_addresses",
            sa.Column("allocation_type", sa.String(length=20), nullable=True),
        )


def downgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)

    if _column_exists(inspector, "ipv4_addresses", "allocation_type"):
        op.drop_column("ipv4_addresses", "allocation_type")

    if _index_exists(inspector, "ipv4_addresses", "ix_ipv4_addresses_ont_unit_id"):
        op.drop_index("ix_ipv4_addresses_ont_unit_id", table_name="ipv4_addresses")

    if _fk_exists(
        inspector, "ipv4_addresses", "fk_ipv4_addresses_ont_unit_id_ont_units"
    ):
        op.drop_constraint(
            "fk_ipv4_addresses_ont_unit_id_ont_units",
            "ipv4_addresses",
            type_="foreignkey",
        )

    if _column_exists(inspector, "ipv4_addresses", "ont_unit_id"):
        op.drop_column("ipv4_addresses", "ont_unit_id")
