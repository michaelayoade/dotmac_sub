"""Add network zones table and zone_id FK columns.

Revision ID: w3x4y5z6a7b8
Revises: v2w3x4y5z6a7
Create Date: 2026-02-24
"""

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import UUID

from alembic import op

revision = "w3x4y5z6a7b8"
down_revision = "v2w3x4y5z6a7"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Create network_zones table (idempotent)
    conn = op.get_bind()
    inspector = sa.inspect(conn)

    if not inspector.has_table("network_zones"):
        op.create_table(
            "network_zones",
            sa.Column("id", UUID(as_uuid=True), primary_key=True),
            sa.Column("name", sa.String(100), nullable=False),
            sa.Column("description", sa.Text, nullable=True),
            sa.Column("parent_id", UUID(as_uuid=True), sa.ForeignKey("network_zones.id"), nullable=True),
            sa.Column("latitude", sa.Float, nullable=True),
            sa.Column("longitude", sa.Float, nullable=True),
            sa.Column("is_active", sa.Boolean, default=True, server_default=sa.text("true")),
            sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
            sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
            sa.UniqueConstraint("name", name="uq_network_zones_name"),
        )

    # Add PostGIS geometry column if PostGIS is available
    try:
        op.execute(
            "ALTER TABLE network_zones ADD COLUMN IF NOT EXISTS "
            "geom geometry(POINT, 4326)"
        )
    except Exception:
        pass  # PostGIS not available — skip geometry column

    # Add zone_id FK columns to existing tables (idempotent)
    columns = inspector.get_columns("ont_units")
    ont_cols = {c["name"] for c in columns}
    if "zone_id" not in ont_cols:
        op.add_column("ont_units", sa.Column("zone_id", UUID(as_uuid=True), nullable=True))
        op.create_foreign_key(
            "fk_ont_units_zone_id",
            "ont_units",
            "network_zones",
            ["zone_id"],
            ["id"],
        )

    columns = inspector.get_columns("splitters")
    splitter_cols = {c["name"] for c in columns}
    if "zone_id" not in splitter_cols:
        op.add_column("splitters", sa.Column("zone_id", UUID(as_uuid=True), nullable=True))
        op.create_foreign_key(
            "fk_splitters_zone_id",
            "splitters",
            "network_zones",
            ["zone_id"],
            ["id"],
        )

    columns = inspector.get_columns("fdh_cabinets")
    fdh_cols = {c["name"] for c in columns}
    if "zone_id" not in fdh_cols:
        op.add_column("fdh_cabinets", sa.Column("zone_id", UUID(as_uuid=True), nullable=True))
        op.create_foreign_key(
            "fk_fdh_cabinets_zone_id",
            "fdh_cabinets",
            "network_zones",
            ["zone_id"],
            ["id"],
        )

    if inspector.has_table("pop_sites"):
        columns = inspector.get_columns("pop_sites")
        pop_cols = {c["name"] for c in columns}
        if "zone_id" not in pop_cols:
            op.add_column("pop_sites", sa.Column("zone_id", UUID(as_uuid=True), nullable=True))
            op.create_foreign_key(
                "fk_pop_sites_zone_id",
                "pop_sites",
                "network_zones",
                ["zone_id"],
                ["id"],
            )


def downgrade() -> None:
    conn = op.get_bind()
    inspector = sa.inspect(conn)

    # Drop zone_id FK columns
    if inspector.has_table("pop_sites"):
        columns = inspector.get_columns("pop_sites")
        if "zone_id" in {c["name"] for c in columns}:
            op.drop_constraint("fk_pop_sites_zone_id", "pop_sites", type_="foreignkey")
            op.drop_column("pop_sites", "zone_id")

    columns = inspector.get_columns("fdh_cabinets")
    if "zone_id" in {c["name"] for c in columns}:
        op.drop_constraint("fk_fdh_cabinets_zone_id", "fdh_cabinets", type_="foreignkey")
        op.drop_column("fdh_cabinets", "zone_id")

    columns = inspector.get_columns("splitters")
    if "zone_id" in {c["name"] for c in columns}:
        op.drop_constraint("fk_splitters_zone_id", "splitters", type_="foreignkey")
        op.drop_column("splitters", "zone_id")

    columns = inspector.get_columns("ont_units")
    if "zone_id" in {c["name"] for c in columns}:
        op.drop_constraint("fk_ont_units_zone_id", "ont_units", type_="foreignkey")
        op.drop_column("ont_units", "zone_id")

    if inspector.has_table("network_zones"):
        op.drop_table("network_zones")
