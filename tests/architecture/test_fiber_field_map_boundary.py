from __future__ import annotations

import ast
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[2]
SERVICE = PROJECT_ROOT / "app/services/network/fiber_topology_field_map.py"
WORKLIST = PROJECT_ROOT / "app/services/network/fiber_topology_field_worklist.py"
OBSERVATIONS = (
    PROJECT_ROOT / "app/services/network/fiber_topology_field_observations.py"
)
COMMAND = PROJECT_ROOT / "scripts/network/audit_fiber_field_verification_map.py"
ADMIN = PROJECT_ROOT / "app/web/admin/network_fiber_plant.py"
TEMPLATE = PROJECT_ROOT / "templates/admin/network/fiber/field_verification_map.html"


def _tree(path: Path) -> ast.Module:
    return ast.parse(path.read_text())


def _imported_names(tree: ast.Module) -> set[str]:
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            names.update(alias.name for alias in node.names)
        elif isinstance(node, ast.Import):
            names.update(alias.name for alias in node.names)
    return names


def test_field_map_owner_is_exact_read_only_worklist_projection():
    tree = _tree(SERVICE)
    source = SERVICE.read_text()
    imported = _imported_names(tree)
    db_calls = {
        node.func.attr
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and isinstance(node.func.value, ast.Name)
        and node.func.value.id == "db"
    }

    assert db_calls.isdisjoint(
        {"add", "delete", "flush", "commit", "rollback", "execute"}
    )
    assert "reconcile_fiber_field_worklist" in imported
    for forbidden_import in (
        "WorkOrder",
        "FiberTopologyFieldObservation",
        "FiberTopologyIdentityDecision",
        "FiberTopologyConnectivityDecision",
        "FiberChangeRequest",
        "record_fiber_field_observation",
        "propose_identity_decision",
        "propose_connectivity_decision",
    ):
        assert forbidden_import not in imported
    for forbidden_operation in (
        "ST_Distance",
        "ST_ClosestPoint",
        "ST_Snap",
        "nearest",
        "snap_geometry",
        "representative_point",
    ):
        assert forbidden_operation not in source
    assert "feature.geometry_geojson" in source
    assert '"worklist_report_sha256"' in source
    assert '"map_feature_sha256"' in source
    assert "ready_for" not in source
    assert "eligible" not in source


def test_field_map_cohort_queries_do_not_load_unrelated_history():
    worklist_source = WORKLIST.read_text()
    observation_source = OBSERVATIONS.read_text()

    assert "func.row_number()" in worklist_source
    assert ".where(ranked.c.rank == 1)" in worklist_source
    assert "tuple_(" in observation_source
    assert ").in_(keys)" in observation_source
    assert "source_asset_type.in_(asset_types)" not in observation_source


def test_field_map_cli_is_full_cohort_repeatable_and_read_only():
    tree = _tree(COMMAND)
    commands = {
        node.args[0].value
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr == "add_parser"
        and node.args
        and isinstance(node.args[0], ast.Constant)
        and isinstance(node.args[0].value, str)
    }
    source = COMMAND.read_text()

    assert commands == set()
    # The read-only guarantee now comes from the shared seam, which pins
    # REPEATABLE READ and postgresql_readonly together. Asserting the raw
    # SQL would require the broken form this repair removed.
    assert "read_only_snapshot_session()" in source
    assert "return 0" in source
    assert "return 2" not in source
    assert "--limit" not in source
    assert "--profile" not in source
    for forbidden in ("propose", "approve", "execute", "apply"):
        assert f'add_parser("{forbidden}"' not in source


def test_field_map_admin_is_get_only_and_has_no_business_actions():
    admin_source = ADMIN.read_text()
    template_source = TEMPLATE.read_text()

    assert '"/fiber-field-verification-map"' in admin_source
    assert '"/fiber-field-verification-map/data"' in admin_source
    assert "project_fiber_field_verification_map(db).to_dict()" in admin_source
    assert "fetch('/admin/network/fiber-field-verification-map/data'" in template_source
    assert "requestAnimationFrame(addBatch)" in template_source
    assert 'context["field_map"]' not in admin_source
    assert "Color represents only" in template_source
    assert "exact staged source GeoJSON" in template_source
    assert "Filters change only this browser view" in template_source
    for forbidden in (
        "<form",
        'method="post"',
        'type="submit"',
        "/work-orders/create",
        "/source-observations",
        "bulk approve",
    ):
        assert forbidden not in template_source.lower()


def test_phase21_adds_no_schema_migration():
    assert not list((PROJECT_ROOT / "alembic/versions").glob("346*fiber*field*map*.py"))
