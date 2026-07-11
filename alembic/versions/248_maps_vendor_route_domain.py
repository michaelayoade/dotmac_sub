"""Maps §A: native vendor route domain tables.

One Alembic set creating the CRM vendor installation-project / quote / route
domain natively in sub (``dotmac_crm/app/models/vendor.py`` ported to
``app/models/vendor_routes.py``): ``vendors``, ``vendor_users``,
``installation_projects``, ``project_quotes``, ``project_quote_line_items``
(CRM ``quote_line_items`` renamed to avoid the sales-table clash),
``proposed_route_revisions``, ``as_built_routes``, ``as_built_line_items`` and
``installation_project_notes``.

Conventions (same as the Phase 3 §244 set):

* CRM PG enums land as String columns + app-level enums;
* CRM UUID PKs are kept (the backfill upserts ON CONFLICT on the CRM id);
* ``installation_projects.project_id`` is a real FK to native ``projects``;
  ``buildout_project_id`` → ``buildout_projects`` and the route
  ``fiber_segment_id`` columns → ``fiber_segments`` are real FKs too;
* customer party (``installation_projects.subscriber_id``) → ``subscribers``;
* staff / CRM-person columns and ``address_id`` / ``vendor_users.person_id``
  are plain UUIDs (no ``people`` table in sub);
* ``proposed_route_revisions.route_geom`` and ``as_built_routes.route_geom``
  are ``geometry(LINESTRING, 4326)`` (geoalchemy2), each with a GIST index.

The mutual FK between ``installation_projects.approved_quote_id`` and
``project_quotes.project_id`` is broken by adding the ``approved_quote_id`` FK
after ``project_quotes`` exists. All ops are idempotent (guarded by live-schema
inspection) and sqlite early-returns — tests build the schema from model
metadata via ``create_all``.

Revision ID: 248_maps_vendor_route_domain
Revises: 247_merge_phase3_inbox_heads
Create Date: 2026-07-11
"""

from __future__ import annotations

import sqlalchemy as sa
from geoalchemy2 import Geometry
from sqlalchemy.dialects import postgresql

from alembic import op

revision = "248_maps_vendor_route_domain"
down_revision = "247_merge_phase3_inbox_heads"
branch_labels = None
depends_on = None

_APPROVED_QUOTE_FK = "fk_installation_projects_approved_quote"
_PROPOSED_GEOM_IDX = "idx_proposed_route_revisions_route_geom"
_ASBUILT_GEOM_IDX = "idx_as_built_routes_route_geom"


def _inspector():
    return sa.inspect(op.get_bind())


def _has_table(table_name: str) -> bool:
    return table_name in _inspector().get_table_names()


def _has_index(table_name: str, index_name: str) -> bool:
    return any(i["name"] == index_name for i in _inspector().get_indexes(table_name))


def _column_fk_names(table_name: str, column_name: str) -> list[str]:
    return [
        fk["name"]
        for fk in _inspector().get_foreign_keys(table_name)
        if fk["constrained_columns"] == [column_name] and fk["name"]
    ]


def _uuid_pk() -> sa.Column:
    return sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True)


def _timestamps() -> list[sa.Column]:
    return [
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
    ]


def _create_vendors() -> None:
    if _has_table("vendors"):
        return
    op.create_table(
        "vendors",
        _uuid_pk(),
        sa.Column("name", sa.String(length=160), nullable=False),
        sa.Column("code", sa.String(length=60)),
        sa.Column("contact_name", sa.String(length=160)),
        sa.Column("contact_email", sa.String(length=255)),
        sa.Column("contact_phone", sa.String(length=40)),
        sa.Column("license_number", sa.String(length=120)),
        sa.Column("service_area", sa.Text()),
        sa.Column("is_active", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("notes", sa.Text()),
        sa.Column("erp_id", sa.String(length=100)),
        *_timestamps(),
        sa.UniqueConstraint("code", name="uq_vendors_code"),
        sa.UniqueConstraint("erp_id", name="uq_vendors_erp_id"),
    )
    op.create_index("ix_vendors_erp_id", "vendors", ["erp_id"])


def _create_vendor_users() -> None:
    if _has_table("vendor_users"):
        return
    op.create_table(
        "vendor_users",
        _uuid_pk(),
        sa.Column(
            "vendor_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("vendors.id"),
            nullable=False,
        ),
        # CRM person UUID — no people table in sub, plain UUID.
        sa.Column("person_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("role", sa.String(length=60)),
        sa.Column("is_active", sa.Boolean(), nullable=False, server_default=sa.true()),
        *_timestamps(),
        sa.UniqueConstraint(
            "vendor_id", "person_id", name="uq_vendor_users_vendor_person"
        ),
    )


def _create_installation_projects() -> None:
    if _has_table("installation_projects"):
        return
    op.create_table(
        "installation_projects",
        _uuid_pk(),
        # Real FK to sub's now-native projects table (Phase 3).
        sa.Column(
            "project_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("projects.id"),
            nullable=False,
        ),
        sa.Column(
            "buildout_project_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("buildout_projects.id"),
        ),
        sa.Column(
            "subscriber_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("subscribers.id"),
        ),
        # CRM address UUID — no sub correspondence, plain UUID.
        sa.Column("address_id", postgresql.UUID(as_uuid=True)),
        sa.Column(
            "assigned_vendor_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("vendors.id"),
        ),
        sa.Column("assignment_type", sa.String(length=20)),
        sa.Column(
            "status", sa.String(length=40), nullable=False, server_default="draft"
        ),
        sa.Column("bidding_open_at", sa.DateTime(timezone=True)),
        sa.Column("bidding_close_at", sa.DateTime(timezone=True)),
        sa.Column("erp_purchase_order_id", sa.String(length=100)),
        # Mutual FK with project_quotes.project_id — the FK is added after
        # project_quotes exists (see _add_approved_quote_fk).
        sa.Column("approved_quote_id", postgresql.UUID(as_uuid=True)),
        # Staff person — plain UUID.
        sa.Column("created_by_person_id", postgresql.UUID(as_uuid=True)),
        sa.Column("notes", sa.Text()),
        sa.Column("is_active", sa.Boolean(), nullable=False, server_default=sa.true()),
        *_timestamps(),
        sa.UniqueConstraint("project_id", name="uq_installation_projects_project"),
    )
    op.create_index(
        "ix_installation_projects_erp_purchase_order_id",
        "installation_projects",
        ["erp_purchase_order_id"],
    )


def _create_project_quotes() -> None:
    if _has_table("project_quotes"):
        return
    op.create_table(
        "project_quotes",
        _uuid_pk(),
        sa.Column(
            "project_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("installation_projects.id"),
            nullable=False,
        ),
        sa.Column(
            "vendor_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("vendors.id"),
            nullable=False,
        ),
        sa.Column(
            "status", sa.String(length=40), nullable=False, server_default="draft"
        ),
        sa.Column(
            "currency", sa.String(length=3), nullable=False, server_default="NGN"
        ),
        sa.Column("subtotal", sa.Numeric(12, 2), nullable=False, server_default="0"),
        sa.Column("vat_rate_percent", sa.Numeric(5, 2)),
        sa.Column("tax_total", sa.Numeric(12, 2), nullable=False, server_default="0"),
        sa.Column("total", sa.Numeric(12, 2), nullable=False, server_default="0"),
        sa.Column("valid_from", sa.DateTime(timezone=True)),
        sa.Column("valid_until", sa.DateTime(timezone=True)),
        sa.Column("submitted_at", sa.DateTime(timezone=True)),
        sa.Column("reviewed_at", sa.DateTime(timezone=True)),
        # Staff persons — plain UUIDs.
        sa.Column("reviewed_by_person_id", postgresql.UUID(as_uuid=True)),
        sa.Column("review_notes", sa.Text()),
        sa.Column("created_by_person_id", postgresql.UUID(as_uuid=True)),
        sa.Column("is_active", sa.Boolean(), nullable=False, server_default=sa.true()),
        *_timestamps(),
    )


def _create_project_quote_line_items() -> None:
    if _has_table("project_quote_line_items"):
        return
    op.create_table(
        "project_quote_line_items",
        _uuid_pk(),
        sa.Column(
            "quote_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("project_quotes.id"),
            nullable=False,
        ),
        sa.Column("item_type", sa.String(length=80)),
        sa.Column("description", sa.Text()),
        sa.Column("cable_type", sa.String(length=120)),
        sa.Column("fiber_count", sa.Integer()),
        sa.Column("splice_count", sa.Integer()),
        sa.Column("quantity", sa.Numeric(12, 3), nullable=False, server_default="1"),
        sa.Column("unit_price", sa.Numeric(12, 2), nullable=False, server_default="0"),
        sa.Column("amount", sa.Numeric(12, 2), nullable=False, server_default="0"),
        sa.Column("notes", sa.Text()),
        sa.Column("client_ref", postgresql.UUID(as_uuid=True)),
        sa.Column("is_active", sa.Boolean(), nullable=False, server_default=sa.true()),
        *_timestamps(),
        sa.UniqueConstraint(
            "client_ref", name="uq_project_quote_line_items_client_ref"
        ),
    )
    op.create_index(
        "ix_project_quote_line_items_client_ref",
        "project_quote_line_items",
        ["client_ref"],
    )


def _create_proposed_route_revisions() -> None:
    if _has_table("proposed_route_revisions"):
        return
    op.create_table(
        "proposed_route_revisions",
        _uuid_pk(),
        sa.Column(
            "quote_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("project_quotes.id"),
            nullable=False,
        ),
        sa.Column("revision_number", sa.Integer(), nullable=False),
        sa.Column(
            "status", sa.String(length=40), nullable=False, server_default="draft"
        ),
        sa.Column(
            "route_geom",
            Geometry("LINESTRING", srid=4326, spatial_index=False),
        ),
        sa.Column("length_meters", sa.Float()),
        sa.Column("submitted_at", sa.DateTime(timezone=True)),
        # Staff persons — plain UUIDs.
        sa.Column("submitted_by_person_id", postgresql.UUID(as_uuid=True)),
        sa.Column("reviewed_at", sa.DateTime(timezone=True)),
        sa.Column("reviewed_by_person_id", postgresql.UUID(as_uuid=True)),
        sa.Column("review_notes", sa.Text()),
        sa.Column(
            "fiber_segment_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("fiber_segments.id"),
        ),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint(
            "quote_id", "revision_number", name="uq_proposed_route_quote_revision"
        ),
    )
    op.create_index(
        _PROPOSED_GEOM_IDX,
        "proposed_route_revisions",
        ["route_geom"],
        postgresql_using="gist",
    )


def _create_as_built_routes() -> None:
    if _has_table("as_built_routes"):
        return
    op.create_table(
        "as_built_routes",
        _uuid_pk(),
        sa.Column(
            "project_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("installation_projects.id"),
            nullable=False,
        ),
        sa.Column(
            "proposed_revision_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("proposed_route_revisions.id"),
        ),
        sa.Column(
            "status", sa.String(length=40), nullable=False, server_default="submitted"
        ),
        sa.Column(
            "route_geom",
            Geometry("LINESTRING", srid=4326, spatial_index=False),
        ),
        sa.Column("actual_length_meters", sa.Float()),
        sa.Column("submitted_at", sa.DateTime(timezone=True)),
        # Staff persons — plain UUIDs.
        sa.Column("submitted_by_person_id", postgresql.UUID(as_uuid=True)),
        sa.Column("reviewed_at", sa.DateTime(timezone=True)),
        sa.Column("reviewed_by_person_id", postgresql.UUID(as_uuid=True)),
        sa.Column("review_notes", sa.Text()),
        sa.Column(
            "fiber_segment_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("fiber_segments.id"),
        ),
        sa.Column("report_file_path", sa.String(length=500)),
        sa.Column("report_file_name", sa.String(length=255)),
        sa.Column("report_generated_at", sa.DateTime(timezone=True)),
        sa.Column("variation_type", sa.String(length=40)),
        sa.Column("variation_reason", sa.Text()),
        sa.Column("version", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("work_order_ref", sa.String(length=120)),
        sa.Column("erp_sync_status", sa.String(length=40)),
        sa.Column("erp_reference", sa.String(length=120)),
        sa.Column("erp_sync_at", sa.DateTime(timezone=True)),
        *_timestamps(),
    )
    op.create_index(
        _ASBUILT_GEOM_IDX,
        "as_built_routes",
        ["route_geom"],
        postgresql_using="gist",
    )


def _create_as_built_line_items() -> None:
    if _has_table("as_built_line_items"):
        return
    op.create_table(
        "as_built_line_items",
        _uuid_pk(),
        sa.Column(
            "as_built_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("as_built_routes.id"),
            nullable=False,
        ),
        sa.Column("item_type", sa.String(length=80)),
        sa.Column("description", sa.Text()),
        sa.Column("cable_type", sa.String(length=120)),
        sa.Column("fiber_count", sa.Integer()),
        sa.Column("splice_count", sa.Integer()),
        sa.Column("quantity", sa.Numeric(12, 3), nullable=False, server_default="1"),
        sa.Column("unit_price", sa.Numeric(12, 2), nullable=False, server_default="0"),
        sa.Column("amount", sa.Numeric(12, 2), nullable=False, server_default="0"),
        sa.Column("notes", sa.Text()),
        sa.Column("is_active", sa.Boolean(), nullable=False, server_default=sa.true()),
        *_timestamps(),
    )
    op.create_index(
        "ix_as_built_line_items_as_built_id", "as_built_line_items", ["as_built_id"]
    )


def _create_installation_project_notes() -> None:
    if _has_table("installation_project_notes"):
        return
    op.create_table(
        "installation_project_notes",
        _uuid_pk(),
        sa.Column(
            "project_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("installation_projects.id"),
            nullable=False,
        ),
        # Staff person — plain UUID.
        sa.Column("author_person_id", postgresql.UUID(as_uuid=True)),
        sa.Column("body", sa.Text(), nullable=False),
        sa.Column(
            "is_internal", sa.Boolean(), nullable=False, server_default=sa.false()
        ),
        sa.Column("attachments", sa.JSON()),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )


def _add_approved_quote_fk() -> None:
    if _has_table("installation_projects") and not _column_fk_names(
        "installation_projects", "approved_quote_id"
    ):
        op.create_foreign_key(
            _APPROVED_QUOTE_FK,
            "installation_projects",
            "project_quotes",
            ["approved_quote_id"],
            ["id"],
        )


def upgrade() -> None:
    if op.get_bind().dialect.name == "sqlite":
        return

    # FK creation order.
    _create_vendors()
    _create_vendor_users()
    _create_installation_projects()
    _create_project_quotes()
    _create_project_quote_line_items()
    _create_proposed_route_revisions()
    _create_as_built_routes()
    _create_as_built_line_items()
    _create_installation_project_notes()

    # Close the installation_projects ↔ project_quotes mutual FK.
    _add_approved_quote_fk()


def downgrade() -> None:
    if op.get_bind().dialect.name == "sqlite":
        return

    # Drop the mutual FK first so the tables can be dropped in FK order.
    for fk_name in _column_fk_names("installation_projects", "approved_quote_id"):
        op.drop_constraint(fk_name, "installation_projects", type_="foreignkey")

    # Reverse FK order; DROP TABLE drops each table's indexes with it.
    for table_name in (
        "installation_project_notes",
        "as_built_line_items",
        "as_built_routes",
        "proposed_route_revisions",
        "project_quote_line_items",
        "project_quotes",
        "installation_projects",
        "vendor_users",
        "vendors",
    ):
        if _has_table(table_name):
            op.drop_table(table_name)
