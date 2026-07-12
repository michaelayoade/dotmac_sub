"""Maps §A: native vendor route domain — schema-shape assertions.

Covers the ported table map, the exact CRM enum vocabularies, the String (not
PG enum) status columns, the FK-clash treatment (real FK to native
``projects`` / ``buildout_projects`` / ``fiber_segments`` / ``subscribers``;
staff / person columns as plain UUIDs), the ``route_geom`` geometry column
type, and the 248 migration's revision chain / single head.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path
from uuid import uuid4

import pytest
from geoalchemy2 import Geometry

import app.models  # noqa: F401 — registers every model on Base.metadata
from app.db import Base
from app.models.subscriber import Subscriber
from app.models.vendor_routes import (
    AsBuiltLineItem,
    AsBuiltRoute,
    AsBuiltRouteStatus,
    InstallationProject,
    InstallationProjectNote,
    InstallationProjectStatus,
    ProjectQuote,
    ProjectQuoteLineItem,
    ProjectQuoteStatus,
    ProposedRouteRevision,
    ProposedRouteRevisionStatus,
    VariationType,
    Vendor,
    VendorAssignmentType,
    VendorUser,
)

REPO_ROOT = Path(__file__).resolve().parent.parent

# Every table the maps §A migration creates, named exactly.
MAPS_TABLES = [
    "vendors",
    "vendor_users",
    "installation_projects",
    "project_quotes",
    "project_quote_line_items",  # CRM quote_line_items renamed (sales clash)
    "proposed_route_revisions",
    "as_built_routes",
    "as_built_line_items",
    "installation_project_notes",
]


# ---------------------------------------------------------------------------
# Registration
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("table_name", MAPS_TABLES)
def test_maps_table_registered(table_name):
    assert table_name in Base.metadata.tables


def test_field_vendor_auth_mirror_untouched():
    """The Phase-2 vendor auth mirror coexists — not duplicated/renamed."""
    for table in (
        "field_vendors",
        "field_vendor_users",
        "field_vendor_device_tokens",
    ):
        assert table in Base.metadata.tables


def test_sales_quote_line_items_not_clobbered():
    """Sub's sales quote_line_items stays; the vendor line table is renamed."""
    assert "quote_line_items" in Base.metadata.tables
    assert "project_quote_line_items" in Base.metadata.tables
    # They are distinct tables.
    sales = Base.metadata.tables["quote_line_items"]
    vendor = Base.metadata.tables["project_quote_line_items"]
    assert sales is not vendor
    # The sales table FKs quotes; the vendor table FKs project_quotes.
    assert {fk.target_fullname for fk in sales.columns["quote_id"].foreign_keys} == {
        "quotes.id"
    }
    assert {fk.target_fullname for fk in vendor.columns["quote_id"].foreign_keys} == {
        "project_quotes.id"
    }


# ---------------------------------------------------------------------------
# Enum vocabularies — exact CRM values
# ---------------------------------------------------------------------------


def _values(enum_cls) -> list[str]:
    return [member.value for member in enum_cls]


def test_enum_vocabularies_exact():
    assert _values(VendorAssignmentType) == ["bidding", "direct"]
    assert _values(InstallationProjectStatus) == [
        "draft",
        "open_for_bidding",
        "quoted",
        "approved",
        "in_progress",
        "completed",
        "verified",
        "assigned",
    ]
    assert _values(ProjectQuoteStatus) == [
        "draft",
        "submitted",
        "under_review",
        "approved",
        "rejected",
        "revision_requested",
    ]
    assert _values(ProposedRouteRevisionStatus) == [
        "draft",
        "submitted",
        "accepted",
        "rejected",
    ]
    assert _values(VariationType) == [
        "scope_change",
        "route_deviation",
        "material_change",
        "additional_work",
        "reduction",
    ]
    assert _values(AsBuiltRouteStatus) == [
        "submitted",
        "under_review",
        "accepted",
        "rejected",
    ]


def test_status_columns_are_strings_not_pg_enums():
    from sqlalchemy import Enum as SaEnum
    from sqlalchemy import String as SaString

    checks = [
        ("installation_projects", "status"),
        ("installation_projects", "assignment_type"),
        ("project_quotes", "status"),
        ("proposed_route_revisions", "status"),
        ("as_built_routes", "status"),
        ("as_built_routes", "variation_type"),
    ]
    for table_name, column_name in checks:
        column = Base.metadata.tables[table_name].columns[column_name]
        assert isinstance(column.type, SaString), (table_name, column_name)
        assert not isinstance(column.type, SaEnum), (table_name, column_name)


# ---------------------------------------------------------------------------
# FK clash treatment
# ---------------------------------------------------------------------------


def _fk_targets(table_name: str, column_name: str) -> set[str]:
    column = Base.metadata.tables[table_name].columns[column_name]
    return {fk.target_fullname for fk in column.foreign_keys}


def test_installation_project_id_is_real_fk_to_native_projects():
    """The keystone decision: project_id → sub's now-native projects.id."""
    assert _fk_targets("installation_projects", "project_id") == {"projects.id"}
    assert (
        not Base.metadata.tables["installation_projects"].columns["project_id"].nullable
    )


def test_intra_and_native_fks():
    assert _fk_targets("installation_projects", "buildout_project_id") == {
        "buildout_projects.id"
    }
    assert _fk_targets("installation_projects", "subscriber_id") == {"subscribers.id"}
    assert _fk_targets("installation_projects", "assigned_vendor_id") == {"vendors.id"}
    assert _fk_targets("installation_projects", "approved_quote_id") == {
        "project_quotes.id"
    }
    assert _fk_targets("vendor_users", "vendor_id") == {"vendors.id"}
    assert _fk_targets("project_quotes", "project_id") == {"installation_projects.id"}
    assert _fk_targets("project_quotes", "vendor_id") == {"vendors.id"}
    assert _fk_targets("project_quote_line_items", "quote_id") == {"project_quotes.id"}
    assert _fk_targets("proposed_route_revisions", "quote_id") == {"project_quotes.id"}
    assert _fk_targets("proposed_route_revisions", "fiber_segment_id") == {
        "fiber_segments.id"
    }
    assert _fk_targets("as_built_routes", "project_id") == {"installation_projects.id"}
    assert _fk_targets("as_built_routes", "proposed_revision_id") == {
        "proposed_route_revisions.id"
    }
    assert _fk_targets("as_built_routes", "fiber_segment_id") == {"fiber_segments.id"}
    assert _fk_targets("as_built_line_items", "as_built_id") == {"as_built_routes.id"}
    assert _fk_targets("installation_project_notes", "project_id") == {
        "installation_projects.id"
    }


# Staff / CRM-person columns and the address UUID: FKs dropped, plain UUIDs.
NO_FK_COLUMNS = [
    ("vendor_users", "person_id"),
    ("installation_projects", "address_id"),
    ("installation_projects", "created_by_person_id"),
    ("project_quotes", "reviewed_by_person_id"),
    ("project_quotes", "created_by_person_id"),
    ("proposed_route_revisions", "submitted_by_person_id"),
    ("proposed_route_revisions", "reviewed_by_person_id"),
    ("as_built_routes", "submitted_by_person_id"),
    ("as_built_routes", "reviewed_by_person_id"),
    ("installation_project_notes", "author_person_id"),
]


@pytest.mark.parametrize("table_name,column_name", NO_FK_COLUMNS)
def test_staff_and_person_columns_are_plain_uuids(table_name, column_name):
    assert _fk_targets(table_name, column_name) == set(), (table_name, column_name)


# ---------------------------------------------------------------------------
# Geometry
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("table_name", ["proposed_route_revisions", "as_built_routes"])
def test_route_geom_is_a_geometry_column(table_name):
    # geoalchemy2 rewrites the shared Geometry type's geometry_type/srid during
    # sqlite ``create_all`` (test artifact), so LINESTRING/4326 is asserted from
    # the migration source below; here we only assert it's a Geometry column.
    column = Base.metadata.tables[table_name].columns["route_geom"]
    assert isinstance(column.type, Geometry)


def test_route_geom_declared_linestring_4326_in_migration():
    source = (
        REPO_ROOT / "alembic" / "versions" / "248_maps_vendor_route_domain.py"
    ).read_text()
    # Both route_geom columns are LINESTRING/4326 on Postgres.
    assert source.count('Geometry("LINESTRING", srid=4326') == 2


# ---------------------------------------------------------------------------
# Constraints / indexes preserved
# ---------------------------------------------------------------------------


def _constraint_names(table_name: str) -> set[str]:
    return {c.name for c in Base.metadata.tables[table_name].constraints if c.name}


def test_ported_unique_constraints():
    assert "uq_installation_projects_project" in _constraint_names(
        "installation_projects"
    )
    assert "uq_vendor_users_vendor_person" in _constraint_names("vendor_users")
    assert "uq_proposed_route_quote_revision" in _constraint_names(
        "proposed_route_revisions"
    )
    assert Base.metadata.tables["vendors"].columns["code"].unique
    assert Base.metadata.tables["vendors"].columns["erp_id"].unique
    assert Base.metadata.tables["project_quote_line_items"].columns["client_ref"].unique


# ---------------------------------------------------------------------------
# Migration 248 — revision chain + single head
# ---------------------------------------------------------------------------


def _load_migration():
    path = REPO_ROOT / "alembic" / "versions" / "248_maps_vendor_route_domain.py"
    spec = importlib.util.spec_from_file_location("migration_248", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_migration_248_revision_chain():
    module = _load_migration()
    assert module.revision == "248_maps_vendor_route_domain"
    assert module.down_revision == "247_merge_phase3_inbox_heads"
    assert callable(module.upgrade)
    assert callable(module.downgrade)


def test_migration_248_is_single_alembic_head():
    from alembic.config import Config
    from alembic.script import ScriptDirectory

    config = Config(str(REPO_ROOT / "alembic.ini"))
    config.set_main_option("script_location", str(REPO_ROOT / "alembic"))
    script = ScriptDirectory.from_config(config)
    # 248 is a single link in the chain; the current head advances as later PRs
    # stack on top (Phase 5 asset inventory is the current head).
    assert script.get_heads() == ["259_campaign_ai_workqueue"]


def test_migration_248_creates_geometry_gist_indexes():
    source = (
        REPO_ROOT / "alembic" / "versions" / "248_maps_vendor_route_domain.py"
    ).read_text()
    assert 'postgresql_using="gist"' in source
    assert "idx_proposed_route_revisions_route_geom" in source
    assert "idx_as_built_routes_route_geom" in source


# ---------------------------------------------------------------------------
# End-to-end model wiring (sqlite create_all via the db_session fixture)
# ---------------------------------------------------------------------------


def _subscriber(db_session) -> Subscriber:
    subscriber = Subscriber(
        first_name="Ngozi",
        last_name="Okafor",
        email=f"ngozi-{uuid4().hex[:10]}@example.com",
    )
    db_session.add(subscriber)
    db_session.flush()
    return subscriber


def test_vendor_route_chain_persists(db_session):
    """Vendor → installation project (→ native project) → quote → route round-trips."""
    from app.models.project import Project

    subscriber = _subscriber(db_session)
    project = Project(name="Fiber install — Ngozi", subscriber_id=subscriber.id)
    db_session.add(project)
    db_session.flush()

    vendor = Vendor(name="Skyline Fiber Ltd", code="SKY", erp_id="SUP-001")
    db_session.add(vendor)
    db_session.flush()
    db_session.add(VendorUser(vendor_id=vendor.id, person_id=uuid4(), role="lead"))
    db_session.flush()

    install = InstallationProject(
        project_id=project.id,
        subscriber_id=subscriber.id,
        assigned_vendor_id=vendor.id,
        assignment_type=VendorAssignmentType.direct.value,
        created_by_person_id=uuid4(),
    )
    db_session.add(install)
    db_session.flush()
    assert install.status == InstallationProjectStatus.draft.value

    quote = ProjectQuote(
        project_id=install.id,
        vendor_id=vendor.id,
        total=250000,
        created_by_person_id=uuid4(),
    )
    db_session.add(quote)
    db_session.flush()
    assert quote.status == ProjectQuoteStatus.draft.value
    db_session.add(
        ProjectQuoteLineItem(
            quote_id=quote.id,
            description="Aerial fiber run",
            cable_type="ADSS-24F",
            fiber_count=24,
            quantity=1,
            unit_price=250000,
            amount=250000,
            client_ref=uuid4(),
        )
    )
    db_session.flush()

    install.approved_quote_id = quote.id
    db_session.flush()
    assert install.quotes[0].id == quote.id
    assert install.approved_quote.id == quote.id

    revision = ProposedRouteRevision(
        quote_id=quote.id,
        revision_number=1,
        submitted_by_person_id=uuid4(),
    )
    db_session.add(revision)
    db_session.flush()
    assert revision.status == ProposedRouteRevisionStatus.draft.value

    as_built = AsBuiltRoute(
        project_id=install.id,
        proposed_revision_id=revision.id,
        variation_type=VariationType.route_deviation.value,
        submitted_by_person_id=uuid4(),
    )
    db_session.add(as_built)
    db_session.flush()
    assert as_built.status == AsBuiltRouteStatus.submitted.value
    assert as_built.version == 1
    db_session.add(
        AsBuiltLineItem(
            as_built_id=as_built.id, description="Splice closure", amount=5000
        )
    )
    db_session.add(
        InstallationProjectNote(
            project_id=install.id, body="Route approved", author_person_id=uuid4()
        )
    )
    db_session.flush()

    assert as_built.line_items[0].as_built_id == as_built.id
    assert install.project_notes[0].body == "Route approved"
    assert install.as_built_routes[0].id == as_built.id
