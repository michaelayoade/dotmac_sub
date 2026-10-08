"""Add configurable circular customer regions."""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision = "648_customer_regions"
down_revision = "647_churn_report_lifecycle_indexes"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "customer_regions",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("name", sa.String(length=160), nullable=False),
        sa.Column("latitude", sa.Float(), nullable=False),
        sa.Column("longitude", sa.Float(), nullable=False),
        sa.Column("radius_meters", sa.Float(), nullable=False, server_default="300"),
        sa.Column(
            "color", sa.String(length=7), nullable=False, server_default="#0ea5e9"
        ),
        sa.Column(
            "match_mode", sa.String(length=20), nullable=False, server_default="nearest"
        ),
        sa.Column("priority", sa.Integer(), nullable=False, server_default="0"),
        sa.Column(
            "nas_device_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("nas_devices.id", ondelete="SET NULL"),
        ),
        sa.Column(
            "pop_site_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("pop_sites.id", ondelete="SET NULL"),
        ),
        sa.Column("notes", sa.Text()),
        sa.Column("is_active", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "latitude >= -90 AND latitude <= 90", name="ck_customer_region_latitude"
        ),
        sa.CheckConstraint(
            "longitude >= -180 AND longitude <= 180",
            name="ck_customer_region_longitude",
        ),
        sa.CheckConstraint("radius_meters > 0", name="ck_customer_region_radius"),
        sa.CheckConstraint("length(color) = 7", name="ck_customer_region_color"),
        sa.CheckConstraint(
            "match_mode IN ('nearest', 'nas', 'pop_site', 'manual')",
            name="ck_customer_region_match_mode",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("name", name="uq_customer_regions_name"),
    )
    op.create_index("ix_customer_regions_active", "customer_regions", ["is_active"])
    op.create_index("ix_customer_regions_pop_site", "customer_regions", ["pop_site_id"])
    op.create_index(
        "ix_customer_regions_nas_device", "customer_regions", ["nas_device_id"]
    )


def downgrade() -> None:
    op.drop_index("ix_customer_regions_nas_device", table_name="customer_regions")
    op.drop_index("ix_customer_regions_pop_site", table_name="customer_regions")
    op.drop_index("ix_customer_regions_active", table_name="customer_regions")
    op.drop_table("customer_regions")
