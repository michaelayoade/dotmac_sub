"""Every literal route permission must be present in the RBAC seed catalogue."""

from __future__ import annotations

import ast
import pathlib

from scripts.seed.seed_rbac import DEFAULT_PERMISSIONS

_GUARD_BUILDERS = {
    "require_permission",
    "require_scoped_permission",
    "require_any_permission",
}


def _repo_root() -> pathlib.Path:
    return pathlib.Path(__file__).resolve().parents[2]


def _called_name(call: ast.Call) -> str | None:
    if isinstance(call.func, ast.Name):
        return call.func.id
    if isinstance(call.func, ast.Attribute):
        return call.func.attr
    return None


def _literal_strings(node: ast.AST) -> set[str]:
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return {node.value}
    if isinstance(node, (ast.List, ast.Tuple, ast.Set)):
        return {value for item in node.elts for value in _literal_strings(item)}
    return set()


def _route_permissions() -> set[str]:
    permissions: set[str] = set()
    for path in (_repo_root() / "app").rglob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if (
                not isinstance(node, ast.Call)
                or _called_name(node) not in _GUARD_BUILDERS
            ):
                continue
            for argument in [*node.args, *(item.value for item in node.keywords)]:
                permissions.update(_literal_strings(argument))
    return permissions


def test_route_permissions_are_seeded() -> None:
    seeded = {key for key, _description in DEFAULT_PERMISSIONS}
    missing = _route_permissions() - seeded
    assert not missing, (
        "Route guards reference ungrantable permissions absent from "
        f"DEFAULT_PERMISSIONS: {sorted(missing)}"
    )
