"""Keep scheduled billing on its FK-compatible lock and retry boundary."""

from __future__ import annotations

import ast
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[2]
ACCOUNT_LIFECYCLE = PROJECT_ROOT / "app" / "services" / "account_lifecycle.py"
SCHEDULED_BILLING = PROJECT_ROOT / "app" / "services" / "billing" / "scheduled.py"


def _module_function(path: Path, function_name: str) -> ast.FunctionDef:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    return next(
        node
        for node in tree.body
        if isinstance(node, ast.FunctionDef) and node.name == function_name
    )


def test_anchor_writer_uses_foreign_key_compatible_row_lock() -> None:
    writer = _module_function(
        ACCOUNT_LIFECYCLE,
        "stage_subscription_billing_anchor",
    )
    lock_calls = [
        node
        for node in ast.walk(writer)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr == "with_for_update"
    ]

    assert len(lock_calls) == 1
    key_share = next(
        (
            keyword.value
            for keyword in lock_calls[0].keywords
            if keyword.arg == "key_share"
        ),
        None,
    )
    assert isinstance(key_share, ast.Constant)
    assert key_share.value is True


def test_scheduled_invoice_cycle_uses_the_retry_entrypoint() -> None:
    scheduled = _module_function(SCHEDULED_BILLING, "run_invoice_cycle")
    called_attributes = {
        node.func.attr
        for node in ast.walk(scheduled)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
    }

    assert "run_invoice_cycle_with_retry" in called_attributes
    assert "run_invoice_cycle" not in called_attributes
