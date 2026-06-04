from __future__ import annotations

import ast
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parents[1]
_MAIN_SOURCE = _REPO_ROOT / "app" / "main.py"


def _router_specs_from_main() -> list[tuple[str, str, str, str]]:
    tree = ast.parse(_MAIN_SOURCE.read_text())
    specs: list[tuple[str, str, str, str]] = []
    for node in tree.body:
        if not isinstance(node, ast.Assign):
            continue
        names = [target.id for target in node.targets if isinstance(target, ast.Name)]
        if not any(
            name in {"_CORE_ROUTER_SPECS", "_DEFERRED_API_ROUTER_SPECS"}
            for name in names
        ):
            continue
        value = ast.literal_eval(node.value)
        specs.extend(tuple(item) for item in value)
    return sorted(set(specs))


ROUTER_SPECS = _router_specs_from_main()


def _module_source(module_name: str) -> str:
    rel_path = Path(*module_name.split("."))
    module_file = _REPO_ROOT / rel_path.with_suffix(".py")
    package_file = _REPO_ROOT / rel_path / "__init__.py"
    if module_file.exists():
        return module_file.read_text()
    if package_file.exists():
        return package_file.read_text()
    raise AssertionError(f"{module_name} cannot be resolved to a source file")


def _top_level_names(source: str) -> set[str]:
    tree = ast.parse(source)
    names: set[str] = set()
    for node in tree.body:
        if isinstance(node, (ast.Assign, ast.AnnAssign)):
            targets = node.targets if isinstance(node, ast.Assign) else [node.target]
            for target in targets:
                if isinstance(target, ast.Name):
                    names.add(target.id)
        elif isinstance(node, ast.ImportFrom):
            for alias in node.names:
                names.add(alias.asname or alias.name)
        elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            names.add(node.name)
    return names


@pytest.mark.parametrize(
    ("module_name", "attr_name", "_mount_kind", "_dependency_mode"),
    ROUTER_SPECS,
    ids=lambda spec: f"{spec[0]}:{spec[1]}" if isinstance(spec, tuple) else str(spec),
)
def test_main_router_specs_resolve_to_router_objects(
    module_name: str,
    attr_name: str,
    _mount_kind: str,
    _dependency_mode: str,
) -> None:
    source = _module_source(module_name)
    assert attr_name in _top_level_names(source), (
        f"{module_name}.{attr_name} is not declared at module top level"
    )
