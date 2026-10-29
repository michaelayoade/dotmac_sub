"""Prevent the Tax workbench from acquiring policy or transaction ownership."""

import ast
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def test_workbench_uses_only_public_tax_contracts() -> None:
    for path in (ROOT / "app/services").glob("tax_policy_authoring*.py"):
        tree = ast.parse(path.read_text())
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom):
                module = node.module or ""
                assert not module.startswith(("fastapi", "starlette")), path
                if module.startswith("dotmac_tax"):
                    assert module == "dotmac_tax", (path, module)
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
                assert node.func.attr not in {"commit", "rollback", "begin_nested"}, (
                    path
                )


def test_publication_has_one_owner_entry_and_route_has_no_transaction_writer() -> None:
    owner = ast.parse((ROOT / "app/services/tax_policy_authoring.py").read_text())
    entries = [
        node
        for node in ast.walk(owner)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id == "execute_owner_command"
    ]
    assert len(entries) == 1
    adapter = ast.parse((ROOT / "app/web/admin/billing_tax_policy.py").read_text())
    for node in ast.walk(adapter):
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
            assert node.func.attr not in {
                "add",
                "delete",
                "flush",
                "commit",
                "rollback",
                "begin",
                "begin_nested",
            }
