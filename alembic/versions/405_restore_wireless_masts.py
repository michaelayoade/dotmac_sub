"""Restore canonical wireless-mast inventory on upgraded databases.

Revision ID: 405_restore_wireless_masts
Revises: 404_team_inbox_sot_completion

The original table and POP-link migrations were archived when the historical
chain was squashed.  Fresh databases receive the model metadata through the
base migration, but an upgraded production database can therefore reach the
current head without this table.  This forward repair is idempotent for both
cohorts.
"""

from __future__ import annotations

import geoalchemy2
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql
from sqlalchemy.engine.reflection import Inspector

from alembic import op

revision = "405_restore_wireless_masts"
down_revision = "404_team_inbox_sot_completion"
branch_labels = None
depends_on = None

WIRELESS_MAST_STATUS = postgresql.ENUM(
    "active",
    "inactive",
    "maintenance",
    "decommissioned",
    name="wirelessmaststatus",
    create_type=False,
)


def _inspector() -> Inspector:
    return sa.inspect(op.get_bind())


def upgrade() -> None:
    # The squashed initial schema and the archived enum migration both create
    # this type.  ``checkfirst`` also repairs the exceptional upgraded cohort
    # where the table and its enum are both absent.
    WIRELESS_MAST_STATUS.create(op.get_bind(), checkfirst=True)
    inspector = _inspector()
    if "wireless_masts" not in inspector.get_table_names():
        op.create_table(
            "wireless_masts",
            sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
            sa.Column("name", sa.String(length=160), nullable=False),
            sa.Column("latitude", sa.Float(), nullable=False),
            sa.Column("longitude", sa.Float(), nullable=False),
            sa.Column(
                "geom",
                geoalchemy2.types.Geometry(
                    geometry_type="POINT",
                    srid=4326,
                    from_text="ST_GeomFromEWKT",
                    name="geometry",
                ),
            ),
            sa.Column("height_m", sa.Float()),
            sa.Column("structure_type", sa.String(length=80)),
            sa.Column("owner", sa.String(length=160)),
            sa.Column(
                "status",
                WIRELESS_MAST_STATUS,
                nullable=False,
                server_default="active",
            ),
            sa.Column(
                "is_active",
                sa.Boolean(),
                nullable=False,
                server_default=sa.true(),
            ),
            sa.Column("notes", sa.Text()),
            sa.Column("metadata", sa.JSON()),
            sa.Column("pop_site_id", postgresql.UUID(as_uuid=True)),
            sa.Column(
                "created_at",
                sa.DateTime(timezone=True),
                nullable=False,
                server_default=sa.func.now(),
            ),
            sa.Column(
                "updated_at",
                sa.DateTime(timezone=True),
                nullable=False,
                server_default=sa.func.now(),
            ),
            sa.ForeignKeyConstraint(
                ["pop_site_id"],
                ["pop_sites.id"],
                name="fk_wireless_masts_pop_site_id",
            ),
            sa.PrimaryKeyConstraint("id"),
        )
    else:
        columns = {column["name"] for column in inspector.get_columns("wireless_masts")}
        if "pop_site_id" not in columns:
            op.add_column(
                "wireless_masts",
                sa.Column("pop_site_id", postgresql.UUID(as_uuid=True)),
            )

        foreign_keys = {
            foreign_key["name"]
            for foreign_key in _inspector().get_foreign_keys("wireless_masts")
        }
        if "fk_wireless_masts_pop_site_id" not in foreign_keys:
            op.create_foreign_key(
                "fk_wireless_masts_pop_site_id",
                "wireless_masts",
                "pop_sites",
                ["pop_site_id"],
                ["id"],
            )

    indexes = {index["name"] for index in _inspector().get_indexes("wireless_masts")}
    if "idx_wireless_masts_geom" not in indexes:
        op.create_index(
            "idx_wireless_masts_geom",
            "wireless_masts",
            ["geom"],
            postgresql_using="gist",
        )
    if "idx_wireless_masts_status" not in indexes:
        op.create_index("idx_wireless_masts_status", "wireless_masts", ["status"])
    if "idx_wireless_masts_pop_site_id" not in indexes:
        op.create_index(
            "idx_wireless_masts_pop_site_id",
            "wireless_masts",
            ["pop_site_id"],
        )


def downgrade() -> None:
    # Forward-only repair: the table may predate this revision on fresh or
    # previously repaired installations.  Dropping it would destroy canonical
    # inventory without proving this migration created it.
    pass
