"""Add field map asset location provenance.

Revision ID: 238_field_map_asset_location_provenance
Revises: 237_field_vendors
Create Date: 2026-07-09
"""

from __future__ import annotations

import sqlalchemy as sa
from sqlalchemy import inspect
from sqlalchemy.dialects import postgresql

from alembic import op

revision = "238_field_map_asset_location_provenance"
down_revision = "237_field_vendors"
branch_labels = None
depends_on = None


def _has_table(name: str) -> bool:
    return name in inspect(op.get_bind()).get_table_names()


def upgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name == "sqlite":
        return
    if _has_table("field_map_asset_location_provenance"):
        return
    op.create_table(
        "field_map_asset_location_provenance",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("asset_type", sa.String(length=80), nullable=False),
        sa.Column("asset_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("source", sa.String(length=32)),
        sa.Column("accuracy_m", sa.Float()),
        sa.Column("updated_by_principal_id", postgresql.UUID(as_uuid=True)),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint(
            "asset_type",
            "asset_id",
            name="uq_field_map_asset_location_provenance_asset",
        ),
    )


def downgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name == "sqlite":
        return
    if _has_table("field_map_asset_location_provenance"):
        op.drop_table("field_map_asset_location_provenance")
