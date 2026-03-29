"""Add map location indexes.

Revision ID: b9f0d1c6c3f7
Revises: 2eef37e08a0f
Create Date: 2025-02-14 00:00:00.000000
"""

from alembic import op


# revision identifiers, used by Alembic.
revision = "b9f0d1c6c3f7"
down_revision = "2eef37e08a0f"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_index(
        "ix_fdh_cabinets_latitude_longitude",
        "fdh_cabinets",
        ["latitude", "longitude"],
    )
    op.create_index(
        "ix_fdh_cabinets_geom",
        "fdh_cabinets",
        ["geom"],
        postgresql_using="gist",
    )
    op.create_index(
        "ix_fiber_splice_closures_latitude_longitude",
        "fiber_splice_closures",
        ["latitude", "longitude"],
    )
    op.create_index(
        "ix_fiber_splice_closures_geom",
        "fiber_splice_closures",
        ["geom"],
        postgresql_using="gist",
    )
    op.create_index(
        "ix_pop_sites_latitude_longitude",
        "pop_sites",
        ["latitude", "longitude"],
    )
    op.create_index(
        "ix_pop_sites_geom",
        "pop_sites",
        ["geom"],
        postgresql_using="gist",
    )
    op.create_index(
        "ix_addresses_latitude_longitude",
        "addresses",
        ["latitude", "longitude"],
    )
    op.create_index(
        "ix_addresses_geom",
        "addresses",
        ["geom"],
        postgresql_using="gist",
    )


def downgrade() -> None:
    op.drop_index("ix_addresses_geom", table_name="addresses")
    op.drop_index("ix_addresses_latitude_longitude", table_name="addresses")
    op.drop_index("ix_pop_sites_geom", table_name="pop_sites")
    op.drop_index("ix_pop_sites_latitude_longitude", table_name="pop_sites")
    op.drop_index("ix_fiber_splice_closures_geom", table_name="fiber_splice_closures")
    op.drop_index("ix_fiber_splice_closures_latitude_longitude", table_name="fiber_splice_closures")
    op.drop_index("ix_fdh_cabinets_geom", table_name="fdh_cabinets")
    op.drop_index("ix_fdh_cabinets_latitude_longitude", table_name="fdh_cabinets")
