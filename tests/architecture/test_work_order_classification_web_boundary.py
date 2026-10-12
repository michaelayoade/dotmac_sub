"""Classification browser discovery and reads stay behind their exact owners."""

import ast
from pathlib import Path

from app.services.auth_dependencies import permission_requirement
from app.web.admin.work_order_classification import PERMISSION, router

ROOT = Path(__file__).resolve().parents[2]


def test_both_browser_routes_require_the_existing_dispatch_write_permission() -> None:
    assert PERMISSION == "operations:dispatch:write"
    routes = tuple(
        route for route in router.routes if route.path.endswith("/classification")
    )
    assert len(routes) == 2
    for route in routes:
        requirements = tuple(
            permission_requirement(dependency.call)
            for dependency in route.dependant.dependencies
        )
        assert any(
            requirement is not None and PERMISSION in requirement.write_any_of
            for requirement in requirements
        )


def test_browser_adapters_do_not_read_orm_or_reuse_legacy_detail_context() -> None:
    source = ROOT / "app/web/admin/work_order_classification.py"
    tree = ast.parse(source.read_text())
    imports = {
        node.module for node in ast.walk(tree) if isinstance(node, ast.ImportFrom)
    }
    assert not any(
        module is not None and module.startswith("app.models") for module in imports
    )
    assert "app.services.web_dispatch_work_orders" not in imports
    assert not any(
        isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr in {"query", "get", "commit", "rollback", "flush"}
        and isinstance(node.func.value, ast.Name)
        and node.func.value.id == "db"
        for node in ast.walk(tree)
    )
    source_text = source.read_text()
    assert "classification_snapshot(" in source_text
    assert "work_order_commands.classify(db, command=command)" in source_text
    template = (
        ROOT / "templates/admin/dispatch/work_order_classification.html"
    ).read_text()
    assert "components/forms/csrf_input.html" in template
    assert "|safe" not in template
    assert "page.snapshot.eligibility" in template


def test_snapshot_is_a_read_only_typed_owner_query() -> None:
    tree = ast.parse((ROOT / "app/services/work_order_commands.py").read_text())
    owner = next(
        node
        for node in tree.body
        if isinstance(node, ast.ClassDef) and node.name == "WorkOrderCommands"
    )
    query = next(
        node
        for node in owner.body
        if isinstance(node, ast.FunctionDef) and node.name == "classification_snapshot"
    )
    assert not any(
        isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr
        in {"add", "delete", "commit", "rollback", "flush", "begin", "begin_nested"}
        for node in ast.walk(query)
    )
    assert (
        isinstance(query.returns, ast.Name)
        and query.returns.id == "WorkOrderClassificationSnapshot"
    )
