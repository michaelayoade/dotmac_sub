from __future__ import annotations

import ast
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[2]
SERVICE = PROJECT_ROOT / "app/services/network/fiber_topology_field_observations.py"
FIELD_ADAPTER = PROJECT_ROOT / "app/services/field/fiber.py"
FIELD_API = PROJECT_ROOT / "app/api/field/fiber.py"
MIGRATION = PROJECT_ROOT / "alembic/versions/345_fiber_topology_field_observations.py"
IDENTITY_COVERAGE = (
    PROJECT_ROOT / "app/services/network/fiber_topology_identity_coverage.py"
)
CONNECTIVITY_COVERAGE = (
    PROJECT_ROOT / "app/services/network/fiber_topology_connectivity_coverage.py"
)
IDENTITY_TEMPLATE = (
    PROJECT_ROOT / "templates/admin/network/fiber/identity_coverage.html"
)
CONNECTIVITY_TEMPLATE = (
    PROJECT_ROOT / "templates/admin/network/fiber/connectivity_coverage.html"
)


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


def test_field_observation_owner_writes_facts_but_no_decisions_or_topology():
    tree = _tree(SERVICE)
    source = SERVICE.read_text()
    imported = _imported_names(tree)

    for forbidden_import in (
        "FiberTopologyIdentityDecision",
        "FiberTopologyConnectivityDecision",
        "FiberChangeRequest",
        "propose_identity_decision",
        "approve_identity_decision",
        "execute_identity_decision",
        "propose_connectivity_decision",
        "approve_connectivity_decision",
        "execute_connectivity_decision",
        "approve_request",
    ):
        assert forbidden_import not in imported
    for forbidden_constructor in (
        "FdhCabinet(",
        "FiberAccessPoint(",
        "FiberSpliceClosure(",
        "FiberSegment(",
        "FiberTerminationPoint(",
        "OntUnit(",
        "PonPort(",
        "SplitterPort(",
        "FiberTopologyIdentityDecision(",
        "FiberTopologyConnectivityDecision(",
        "FiberChangeRequest(",
    ):
        assert forbidden_constructor not in source
    assert source.count("FiberTopologyFieldObservation(") == 1
    for forbidden_inference in (
        "ST_Distance",
        "nearest",
        "snap_to",
        "gps_latitude",
        "gps_longitude",
    ):
        assert forbidden_inference not in source


def test_field_api_and_job_service_are_thin_owner_adapters():
    adapter_source = FIELD_ADAPTER.read_text()
    api_source = FIELD_API.read_text()

    assert "fiber_topology_field_observations.record_fiber_field_observation(" in (
        adapter_source
    )
    assert "fiber_topology_field_observations.list_fiber_field_observations(" in (
        adapter_source
    )
    assert "FiberTopologyFieldObservation(" not in adapter_source
    assert '"/source-observations"' in api_source
    assert "field_fiber.record_source_observation(" in api_source
    assert "field_fiber.list_source_observations(" in api_source
    for forbidden in ("approve", "execute", "reconcile", "cutover"):
        assert f"source-observations/{forbidden}" not in api_source


def test_coverage_projects_field_evidence_without_using_it_as_a_gate():
    for coverage, template in (
        (IDENTITY_COVERAGE, IDENTITY_TEMPLATE),
        (CONNECTIVITY_COVERAGE, CONNECTIVITY_TEMPLATE),
    ):
        source = coverage.read_text()
        gate_body = source[source.index("    gates = (") : source.index("    ready =")]
        template_source = template.read_text()

        assert "project_field_verification_evidence(db, features)" in source
        assert '"field_verification"' in source
        assert "field_verification_counts" not in gate_body
        assert "Field observations" in template_source
        assert "not cutover gates" in template_source


def test_migration_adds_only_immutable_field_evidence_table():
    tree = _tree(MIGRATION)
    upgrade = next(
        node
        for node in tree.body
        if isinstance(node, ast.FunctionDef) and node.name == "upgrade"
    )
    op_calls = [
        node
        for node in ast.walk(upgrade)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and isinstance(node.func.value, ast.Name)
        and node.func.value.id == "op"
    ]
    migration_source = MIGRATION.read_text()

    assert '_TABLE = "fiber_topology_field_observations"' in migration_source
    assert 'down_revision = "344_fiber_connectivity_batch_control"' in (
        migration_source
    )
    assert [node.func.attr for node in op_calls].count("create_table") == 1
    assert {node.func.attr for node in op_calls} <= {"create_table", "create_index"}
    assert 'ondelete="RESTRICT"' in migration_source
