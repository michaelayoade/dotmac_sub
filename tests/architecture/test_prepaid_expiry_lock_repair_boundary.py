"""Incident repair coordinates the lifecycle owner without a second lock writer."""

from __future__ import annotations

import ast
from pathlib import Path

SOURCE = (
    Path(__file__).resolve().parents[2] / "app/services/prepaid_expiry_lock_repair.py"
)


def test_prepaid_expiry_repair_uses_owner_command_and_lifecycle_participant() -> None:
    tree = ast.parse(SOURCE.read_text(encoding="utf-8"))
    calls = {
        node.func.id
        for node in ast.walk(tree)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
    }
    assert "execute_owner_command" in calls
    assert "suspend_subscription" in calls
    assert "commit" not in calls
    assert "rollback" not in calls
    assert "EnforcementLock" not in calls
