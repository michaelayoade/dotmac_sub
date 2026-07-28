"""ServiceOrder.status has one writer, including administrative recovery."""

from __future__ import annotations

import ast
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[2]
APP_DIR = PROJECT_ROOT / "app"
OWNER = "app/services/service_order_lifecycle.py"


def _imports_service_order_status(tree: ast.Module) -> bool:
    return any(
        isinstance(node, ast.ImportFrom)
        and any(alias.name == "ServiceOrderStatus" for alias in node.names)
        for node in ast.walk(tree)
    )


def _references_service_order_status(node: ast.AST) -> bool:
    return any(
        isinstance(item, ast.Name) and item.id == "ServiceOrderStatus"
        for item in ast.walk(node)
    )


def test_service_order_status_assignments_have_one_owner() -> None:
    offenders: list[str] = []
    for path in sorted(APP_DIR.rglob("*.py")):
        relative = path.relative_to(PROJECT_ROOT).as_posix()
        if relative == OWNER:
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"))
        if not _imports_service_order_status(tree):
            continue
        for node in ast.walk(tree):
            if not isinstance(node, ast.Assign):
                continue
            if not _references_service_order_status(node.value):
                continue
            for target in node.targets:
                if not (
                    isinstance(target, ast.Attribute)
                    and target.attr == "status"
                    and isinstance(target.value, ast.Name)
                    and target.value.id in {"order", "service_order"}
                ):
                    continue
                offenders.append(f"{relative}:{node.lineno}")

    assert offenders == [], (
        "Raw ServiceOrder.status writes outside app.services."
        "service_order_lifecycle: " + ", ".join(offenders)
    )


def test_service_order_owner_covers_business_and_recovery_transitions() -> None:
    source = (PROJECT_ROOT / OWNER).read_text(encoding="utf-8")
    assert "def transition_service_order(" in source
    assert "def restore_recorded_status(" in source
    assert "EventType.service_order_recovered" in source
