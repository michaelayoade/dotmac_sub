"""The catalog expiry adapter must not terminalize a due prepaid renewal."""

from __future__ import annotations

import ast
from pathlib import Path

SOURCE = Path(__file__).resolve().parents[2] / "app/services/catalog/subscriptions.py"


def test_expiry_uses_fixed_contract_end_not_prepaid_billing_anchor() -> None:
    tree = ast.parse(SOURCE.read_text(encoding="utf-8"))
    expiry = next(
        node
        for owner in tree.body
        if isinstance(owner, ast.ClassDef) and owner.name == "Subscriptions"
        for node in owner.body
        if isinstance(node, ast.FunctionDef) and node.name == "expire_subscriptions"
    )
    fields = {node.attr for node in ast.walk(expiry) if isinstance(node, ast.Attribute)}
    assert "end_at" in fields
    assert "next_billing_at" not in fields
