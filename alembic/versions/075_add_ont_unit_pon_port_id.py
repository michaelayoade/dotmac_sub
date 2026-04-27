"""Add pon_port_id FK to ont_units table.

This migration adds pon_port_id directly to OntUnit, moving topology data
from OntAssignment to the ONT itself. This separates physical location
from subscriber binding.

Revision ID: 075_add_ont_unit_pon_port_id
Revises: 074_drop_olt_config_pack_columns
Create Date: 2026-04-27
"""

from __future__ import annotations

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision = "075_add_ont_unit_pon_port_id"
down_revision = "074_drop_olt_config_pack_columns"
branch_labels = None
depends_on = None


def _column_exists(inspector: sa.Inspector, table: str, column: str) -> bool:
    return any(col["name"] == column for col in inspector.get_columns(table))


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)

    if not _column_exists(inspector, "ont_units", "pon_port_id"):
        op.add_column(
            "ont_units",
            sa.Column(
                "pon_port_id",
                postgresql.UUID(as_uuid=True),
                sa.ForeignKey("pon_ports.id", ondelete="SET NULL"),
                nullable=True,
            ),
        )
        op.create_index(
            "ix_ont_units_pon_port_id",
            "ont_units",
            ["pon_port_id"],
        )


def downgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)

    if _column_exists(inspector, "ont_units", "pon_port_id"):
        op.drop_index("ix_ont_units_pon_port_id", table_name="ont_units")
        op.drop_column("ont_units", "pon_port_id")
