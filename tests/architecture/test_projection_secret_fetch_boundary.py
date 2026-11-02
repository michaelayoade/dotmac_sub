"""Single-use redemption has one transaction owner; its adapter never completes it."""

from __future__ import annotations

import ast
from pathlib import Path

SERVICE = Path("app/services/integrations/projection_secret_fetch.py")
API = Path("app/api/integrator_observations.py")


def _function(tree: ast.Module, name: str) -> ast.FunctionDef:
    return next(
        node
        for node in tree.body
        if isinstance(node, ast.FunctionDef) and node.name == name
    )


def test_redemption_has_one_owner_boundary_and_no_nested_completion() -> None:
    tree = ast.parse(SERVICE.read_text())
    boundaries = [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id == "execute_owner_command"
    ]
    assert len(boundaries) == 1
    assert not [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr in {"commit", "rollback", "begin", "begin_nested"}
    ]


def test_fetch_adapter_only_delegates_to_the_typed_owner() -> None:
    function = _function(ast.parse(API.read_text()), "fetch_projection_secret")
    calls = [node for node in ast.walk(function) if isinstance(node, ast.Call)]
    owner_calls = [
        node
        for node in calls
        if isinstance(node.func, ast.Attribute)
        and node.func.attr == "redeem_projection_secret"
    ]
    assert len(owner_calls) == 1
    assert {keyword.arg for keyword in owner_calls[0].keywords} == {"command"}
    assert not [
        node
        for node in calls
        if isinstance(node.func, ast.Attribute)
        and node.func.attr in {"commit", "rollback", "execute", "query", "flush"}
    ]
