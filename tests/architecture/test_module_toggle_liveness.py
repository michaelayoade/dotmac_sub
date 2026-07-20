"""Every operator-visible module switch must gate a route or capability."""

from __future__ import annotations

import ast
import pathlib

from app.services.module_manager import MODULE_KEY_MAP

_MODULE_GATES = {"require_module_enabled", "is_module_enabled"}


def _repo_root() -> pathlib.Path:
    return pathlib.Path(__file__).resolve().parents[2]


def _call_name(call: ast.Call) -> str | None:
    if isinstance(call.func, ast.Name):
        return call.func.id
    if isinstance(call.func, ast.Attribute):
        return call.func.attr
    return None


def _live_modules() -> set[str]:
    live: set[str] = set()
    for path in (_repo_root() / "app").rglob("*.py"):
        if path.as_posix().endswith("app/services/module_manager.py"):
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            call_name = _call_name(node)
            if call_name in _MODULE_GATES and node.args:
                argument = node.args[0]
                if isinstance(argument, ast.Constant) and isinstance(
                    argument.value, str
                ):
                    live.add(argument.value)
            if call_name == "Control":
                for keyword in node.keywords:
                    if (
                        keyword.arg == "owner_module"
                        and isinstance(keyword.value, ast.Constant)
                        and isinstance(keyword.value.value, str)
                    ):
                        live.add(keyword.value.value)
    return live


def test_every_module_toggle_has_a_canonical_consumer() -> None:
    inert = set(MODULE_KEY_MAP) - _live_modules()
    assert not inert, (
        "Operator-visible module toggle(s) have no route or owned capability: "
        f"{sorted(inert)}. Add a canonical consumer or remove the toggle."
    )
