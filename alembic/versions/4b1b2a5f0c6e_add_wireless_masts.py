"""add wireless masts

Revision ID: 4b1b2a5f0c6e
Revises: c7d8a9b0e1f2
Create Date: 2026-01-13 12:30:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import UUID
import geoalchemy2


# revision identifiers, used by Alembic.
revision: str = "4b1b2a5f0c6e"
down_revision: Union[str, None] = "c7d8a9b0e1f2"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    existing_tables = set(inspector.get_table_names())

    if "wireless_masts" not in existing_tables:
        op.create_table(
            "wireless_masts",
            sa.Column("id", UUID(as_uuid=True), primary_key=True),
            sa.Column("name", sa.String(160), nullable=False),
            sa.Column("latitude", sa.Float, nullable=False),
            sa.Column("longitude", sa.Float, nullable=False),
            sa.Column(
                "geom",
                geoalchemy2.types.Geometry(
                    geometry_type="POINT",
                    srid=4326,
                    from_text="ST_GeomFromEWKT",
                    name="geometry",
                ),
                nullable=True,
            ),
            sa.Column("height_m", sa.Float, nullable=True),
            sa.Column("structure_type", sa.String(80), nullable=True),
            sa.Column("owner", sa.String(160), nullable=True),
            sa.Column("status", sa.String(40), nullable=False, server_default="active"),
            sa.Column("is_active", sa.Boolean, nullable=False, server_default=sa.true()),
            sa.Column("notes", sa.Text, nullable=True),
            sa.Column("metadata", sa.JSON, nullable=True),
            sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
            sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        )
    op.execute(
        "CREATE INDEX IF NOT EXISTS idx_wireless_masts_geom ON wireless_masts USING GIST(geom);"
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS idx_wireless_masts_status ON wireless_masts(status);"
    )


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS idx_wireless_masts_status;")
    op.execute("DROP INDEX IF EXISTS idx_wireless_masts_geom;")
    op.drop_table("wireless_masts")
