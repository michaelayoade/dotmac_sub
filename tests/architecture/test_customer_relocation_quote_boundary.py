"""The customer relocation adapter must enter the registered owner once."""

from __future__ import annotations

import ast
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def test_customer_relocation_adapter_uses_canonical_handoff_only() -> None:
    tree = ast.parse((ROOT / "app/api/me.py").read_text(encoding="utf-8"))
    route = next(
        node
        for node in tree.body
        if isinstance(node, ast.FunctionDef)
        and node.name == "my_relocation_quote_prepare"
    )
    calls = {
        node.func.id
        for node in ast.walk(route)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
    }
    assert "prepare_approved_relocation_quote" in calls
    assert "stage_relocation_charge" not in calls
    assert "accept_with_deposit" not in calls


def test_quote_deposit_owner_refuses_relocation_conversion() -> None:
    tree = ast.parse(
        (ROOT / "app/services/quote_deposits.py").read_text(encoding="utf-8")
    )
    payment_page = next(
        node
        for node in tree.body
        if isinstance(node, ast.FunctionDef) and node.name == "quote_payment_page"
    )
    source = ast.get_source_segment(
        (ROOT / "app/services/quote_deposits.py").read_text(encoding="utf-8"),
        payment_page,
    )
    assert source is not None
    assert 'endswith("_relocation")' in source
