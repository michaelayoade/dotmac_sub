from __future__ import annotations

import ast
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[2]
SERVICE = (
    PROJECT_ROOT / "app/services/network/fiber_topology_work_order_evidence_map.py"
)
FIELD_SERVICE = PROJECT_ROOT / "app/services/field/fiber.py"
FIELD_API = PROJECT_ROOT / "app/api/field/fiber.py"


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


def test_work_order_evidence_map_owner_is_exact_and_read_only():
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
    assert "project_fiber_field_verification_map" in imported
    assert "list_fiber_field_observations" in imported
    assert "observation_to_dict" in imported
    for forbidden_import in (
        "FiberTopologyIdentityDecision",
        "FiberTopologyConnectivityDecision",
        "FiberChangeRequest",
        "record_fiber_field_observation",
        "create_work_order",
        "assign_work_order",
    ):
        assert forbidden_import not in imported
    for forbidden_operation in (
        "ST_Distance",
        "ST_ClosestPoint",
        "ST_Snap",
        "nearest",
        "snap_geometry",
        "ready_for",
        "eligible",
    ):
        assert forbidden_operation not in source
    assert "every immutable work-order observation must map to exactly one" in source
    assert 'selected_properties.pop("field_verification"' in source
    assert 'selected_properties.pop("current_work_orders"' in source
    assert 'selected_properties.pop("superseded_work_orders"' in source


def test_field_adapter_scopes_one_native_work_order_before_owner_projection():
    source = FIELD_SERVICE.read_text()

    ensure_call = source.index("ensure_work_order_evidence_map_repeatable_snapshot(")
    profile_call = source.index("_profile_from_principal", ensure_call)
    scoped_call = source.index("_scoped_work_order", profile_call)
    owner_call = source.index("project_fiber_work_order_evidence_map(", scoped_call)

    assert ensure_call < profile_call < scoped_call < owner_call
    assert "expected_work_order_public_id=work_order.public_id" in source


def test_field_work_order_evidence_map_api_is_get_only_and_authenticated():
    tree = _tree(FIELD_API)
    source = FIELD_API.read_text()
    route_methods: list[str] = []
    for node in ast.walk(tree):
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        if node.name != "get_field_fiber_work_order_evidence_map":
            continue
        for decorator in node.decorator_list:
            if isinstance(decorator, ast.Call) and isinstance(
                decorator.func, ast.Attribute
            ):
                route_methods.append(decorator.func.attr)

    assert route_methods == ["get"]
    assert '"/work-order-evidence-map"' in source
    assert "Depends(require_user_auth)" in source
    assert "work_order_id: str = Query" in source
    assert "get_work_order_evidence_map(" in source


def test_phase22_adds_no_schema_migration():
    assert not list(
        (PROJECT_ROOT / "alembic/versions").glob("346*fiber*work*order*map*.py")
    )
