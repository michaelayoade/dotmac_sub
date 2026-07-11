"""Pure-logic tests for the maps §B vendor-route backfill importer.

Covers the FK-driven step order, the geom-aware upsert SQL builder (uuid/json/
geom casts, immutable columns, ST_GeomFromEWKT passthrough), the geometry read
expression, the deferred-link column omissions, and the link-resolution
helpers (native project blocker, nullable native refs, subscriber link key 1).
"""

from __future__ import annotations

from scripts.migration.import_crm_vendor_routes import (
    STEP_ORDER,
    TABLE_SPECS,
    NativeRef,
    build_upsert_sql,
    geom_read_expr,
    resolve_nullable_native,
    resolve_project_id,
    resolve_subscriber,
)

# ---------------------------------------------------------------------------
# Step order
# ---------------------------------------------------------------------------


def test_step_order_is_fk_driven_per_spec() -> None:
    assert STEP_ORDER == (
        "vendors",
        "vendor_users",
        "installation_projects",
        "project_quotes",
        "project_quote_line_items",
        "as_built_routes",
        "as_built_line_items",
        "proposed_route_revisions",
        "installation_project_notes",
        "deferred_links",
    )


def test_step_order_fk_dependencies_hold() -> None:
    index = {step: i for i, step in enumerate(STEP_ORDER)}
    # vendors before their users and before anything that FKs a vendor.
    assert index["vendors"] < index["vendor_users"]
    assert index["vendors"] < index["installation_projects"]
    assert index["vendors"] < index["project_quotes"]
    # installation_projects before its quotes / routes / notes.
    assert index["installation_projects"] < index["project_quotes"]
    assert index["installation_projects"] < index["as_built_routes"]
    assert index["installation_projects"] < index["installation_project_notes"]
    # quotes before their line items and route revisions.
    assert index["project_quotes"] < index["project_quote_line_items"]
    assert index["project_quotes"] < index["proposed_route_revisions"]
    # as_built before its line items.
    assert index["as_built_routes"] < index["as_built_line_items"]
    # deferred links run last (approved_quote / proposed_revision targets exist).
    assert index["deferred_links"] == len(STEP_ORDER) - 1
    assert index["project_quotes"] < index["deferred_links"]
    assert index["proposed_route_revisions"] < index["deferred_links"]


# ---------------------------------------------------------------------------
# Upsert SQL builder (geom-aware)
# ---------------------------------------------------------------------------


def test_build_upsert_sql_casts_and_updates() -> None:
    sql = build_upsert_sql(TABLE_SPECS["vendor_users"])
    assert "INSERT INTO vendor_users" in sql
    assert "CAST(:vendor_id AS uuid)" in sql
    assert "CAST(:person_id AS uuid)" in sql
    assert "ON CONFLICT (id) DO UPDATE SET" in sql
    # id + created_at are immutable, never in the SET clause.
    assert "id = EXCLUDED.id" not in sql
    assert "created_at = EXCLUDED.created_at" not in sql
    assert "role = EXCLUDED.role" in sql


def test_build_upsert_sql_wraps_route_geom_with_ewkt() -> None:
    sql = build_upsert_sql(TABLE_SPECS["as_built_routes"])
    # geometry passthrough: ST_GeomFromEWKT preserves SRID=4326 verbatim.
    assert "ST_GeomFromEWKT(:route_geom)" in sql
    assert "CAST(:route_geom AS uuid)" not in sql
    assert "route_geom = EXCLUDED.route_geom" in sql


def test_build_upsert_sql_casts_json_attachments() -> None:
    sql = build_upsert_sql(TABLE_SPECS["installation_project_notes"])
    assert "CAST(:attachments AS json)" in sql


def test_geom_read_expr_uses_ewkt() -> None:
    assert geom_read_expr("route_geom") == "ST_AsEWKT(route_geom) AS route_geom"


# ---------------------------------------------------------------------------
# Deferred-link column omissions + geom column declarations
# ---------------------------------------------------------------------------


def test_installation_projects_defers_approved_quote() -> None:
    # approved_quote_id FKs project_quotes (imported later) → applied in the
    # deferred pass, so it is absent from the inline upsert columns.
    assert "approved_quote_id" not in TABLE_SPECS["installation_projects"].columns


def test_as_built_routes_defers_proposed_revision() -> None:
    assert "proposed_revision_id" not in TABLE_SPECS["as_built_routes"].columns


def test_route_tables_declare_geom_columns() -> None:
    assert TABLE_SPECS["as_built_routes"].geom_columns == frozenset({"route_geom"})
    assert TABLE_SPECS["proposed_route_revisions"].geom_columns == frozenset(
        {"route_geom"}
    )


def test_table_specs_exist_for_every_import_table() -> None:
    import_steps = tuple(step for step in STEP_ORDER if step != "deferred_links")
    assert set(TABLE_SPECS) == set(import_steps)
    for spec in TABLE_SPECS.values():
        assert "id" in spec.columns
        assert "id" in spec.uuid_columns


# ---------------------------------------------------------------------------
# Link resolution
# ---------------------------------------------------------------------------

PROJECT_IDS = {"proj-1", "proj-2"}


def test_resolve_project_id_direct_match() -> None:
    ref = resolve_project_id("PROJ-1", PROJECT_IDS)
    assert ref == NativeRef("PROJ-1", "resolved")


def test_resolve_project_id_absent_blocks() -> None:
    ref = resolve_project_id("proj-9", PROJECT_IDS)
    assert ref.action == "block"
    assert ref.reason == "project_absent_in_sub"


def test_resolve_project_id_missing_blocks() -> None:
    ref = resolve_project_id(None, PROJECT_IDS)
    assert ref.action == "block"
    assert ref.reason == "missing_project_id"


def test_resolve_nullable_native_hit_miss_and_none() -> None:
    present = {"seg-1"}
    assert resolve_nullable_native("SEG-1", present) == NativeRef("SEG-1", "resolved")
    miss = resolve_nullable_native("seg-9", present)
    assert miss.value is None and miss.action == "null" and miss.reason == "absent_in_sub"
    empty = resolve_nullable_native(None, present)
    assert empty.value is None and empty.action == "null" and empty.reason is None


def test_resolve_subscriber_link_key_1() -> None:
    subscriber_map = {"crm-sub-1": "sub-1"}
    hit = resolve_subscriber("crm-sub-1", subscriber_map)
    assert hit == NativeRef("sub-1", "resolved")
    miss = resolve_subscriber("crm-sub-9", subscriber_map)
    assert miss.value is None and miss.reason == "unmapped_crm_subscriber"
    empty = resolve_subscriber(None, subscriber_map)
    assert empty.value is None and empty.reason is None
