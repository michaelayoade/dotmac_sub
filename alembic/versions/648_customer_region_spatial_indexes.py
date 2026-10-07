"""Add indexes used by customer region assignment queries."""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision = "648_customer_region_spatial_indexes"
down_revision = "647_customer_regions"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # Region matching uses geography distances while address storage remains
    # geometry(POINT, 4326). The expression index keeps ST_DWithin eligible for
    # an indexed spatial plan instead of forcing a full address scan.
    op.execute(
        "CREATE INDEX ix_addresses_geom_geography_customer_regions "
        "ON addresses USING gist ((geom::geography))"
    )
    # The primary-address scalar subquery is evaluated by both list filtering
    # and report assignment. Keep its ordering and subscriber lookup bounded.
    op.create_index(
        "ix_addresses_subscriber_primary_id",
        "addresses",
        ["subscriber_id", "is_primary", "id"],
    )


def downgrade() -> None:
    op.drop_index("ix_addresses_subscriber_primary_id", table_name="addresses")
    op.execute("DROP INDEX ix_addresses_geom_geography_customer_regions")
