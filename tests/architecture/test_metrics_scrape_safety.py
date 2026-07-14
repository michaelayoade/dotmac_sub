"""Architecture guard for the cross-Dotmac metrics scrape contract."""

from __future__ import annotations

import ast
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SAFE_COLLECTOR_SERVICES = {
    "app.services.billing_health",
    "app.services.connectivity_reconciler",
    "app.services.ip_consistency_audit",
    "app.services.observability",
    "app.services.poller_health",
    "app.services.radius_reconciliation",
}
FORBIDDEN_NAMES = {
    "SessionLocal",
    "db_session_adapter",
    "get_db",
}
FORBIDDEN_METHODS = {
    "commit",
    "connect",
    "execute",
    "query",
    "read_session",
    "rollback",
    "scalar",
    "session",
}


def _metrics_route(tree: ast.AST) -> ast.FunctionDef | ast.AsyncFunctionDef:
    for node in ast.walk(tree):
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        for decorator in node.decorator_list:
            if (
                isinstance(decorator, ast.Call)
                and isinstance(decorator.func, ast.Attribute)
                and decorator.func.attr == "get"
                and decorator.args
                and isinstance(decorator.args[0], ast.Constant)
                and decorator.args[0].value == "/metrics"
            ):
                return node
    raise AssertionError("No /metrics route found")


def _violations(node: ast.AST) -> list[str]:
    found: list[str] = []
    for child in ast.walk(node):
        if isinstance(child, ast.Name) and child.id in FORBIDDEN_NAMES:
            found.append(child.id)
        if (
            isinstance(child, ast.Call)
            and isinstance(child.func, ast.Attribute)
            and child.func.attr in FORBIDDEN_METHODS
        ):
            found.append(child.func.attr)
        if isinstance(child, ast.ImportFrom) and (
            child.module == "sqlalchemy"
            or str(child.module).startswith("sqlalchemy.")
            or str(child.module).startswith("app.models")
        ):
            found.append(str(child.module))
    return sorted(set(found))


def test_metrics_route_has_no_database_or_business_work() -> None:
    tree = ast.parse((ROOT / "app/main.py").read_text(encoding="utf-8"))
    route = _metrics_route(tree)
    assert _violations(route) == []
    assert not any(
        isinstance(node, ast.ImportFrom) and str(node.module).startswith("app.services")
        for node in ast.walk(route)
    )


def test_custom_collectors_use_only_approved_snapshot_services() -> None:
    tree = ast.parse((ROOT / "app/metrics.py").read_text(encoding="utf-8"))
    collector_methods: list[ast.FunctionDef | ast.AsyncFunctionDef] = []
    imported_services: set[str] = set()
    for node in tree.body:
        if not isinstance(node, ast.ClassDef) or not node.name.endswith("Collector"):
            continue
        for child in node.body:
            if (
                isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef))
                and child.name == "collect"
            ):
                collector_methods.append(child)
                for descendant in ast.walk(child):
                    if isinstance(descendant, ast.ImportFrom) and str(
                        descendant.module
                    ).startswith("app.services."):
                        imported_services.add(str(descendant.module))

    violations = [
        violation for method in collector_methods for violation in _violations(method)
    ]
    assert sorted(set(violations)) == []
    assert imported_services <= SAFE_COLLECTOR_SERVICES


def test_metrics_scrape_policy_is_checked_in() -> None:
    assert (ROOT / "docs/METRICS_SCRAPE_SAFETY.md").is_file()


def test_prometheus_callbacks_stay_in_reviewed_exporter_module() -> None:
    violations = []
    for path in (ROOT / "app").rglob("*.py"):
        source = path.read_text(encoding="utf-8")
        if (
            "REGISTRY.register(" in source or ".set_function(" in source
        ) and path != ROOT / "app/metrics.py":
            violations.append(str(path.relative_to(ROOT)))
    assert violations == []
