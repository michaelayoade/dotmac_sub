"""Keep service expiry separate from explicit customer account deletion."""

import ast
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
LIFECYCLE = ROOT / "app/services/account_lifecycle.py"


def test_derived_account_status_never_infers_account_cancellation() -> None:
    tree = ast.parse(LIFECYCLE.read_text(encoding="utf-8"))
    derive = next(
        node
        for node in tree.body
        if isinstance(node, ast.FunctionDef)
        and node.name == "derive_account_status_without_override"
    )

    inferred_cancellation = any(
        isinstance(node, ast.Attribute)
        and isinstance(node.value, ast.Name)
        and node.value.id == "SubscriberStatus"
        and node.attr == "canceled"
        for node in ast.walk(derive)
    )

    assert not inferred_cancellation, (
        "Service-derived account status must not mark an account canceled; "
        "account deletion is an explicit transition."
    )
