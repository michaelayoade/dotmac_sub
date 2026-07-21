"""Protect the prepaid enforcement timer participant boundary."""

from __future__ import annotations

import ast
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[2]
APP_ROOT = PROJECT_ROOT / "app"
OWNER = APP_ROOT / "services" / "prepaid_enforcement_state.py"
TIMER_FIELDS = {"prepaid_low_balance_at", "prepaid_deactivation_at"}
OWNER_FUNCTIONS = {
    "arm_prepaid_low_balance_timer",
    "mark_prepaid_deactivated",
    "clear_prepaid_enforcement_timers",
}


def _tree(path: Path) -> ast.AST:
    return ast.parse(path.read_text(encoding="utf-8"), filename=str(path))


def test_prepaid_timer_fields_have_one_application_writer() -> None:
    writers: set[str] = set()
    for path in APP_ROOT.rglob("*.py"):
        for node in ast.walk(_tree(path)):
            if not isinstance(node, (ast.Assign, ast.AnnAssign, ast.AugAssign)):
                continue
            targets = node.targets if isinstance(node, ast.Assign) else [node.target]
            if any(
                isinstance(target, ast.Attribute) and target.attr in TIMER_FIELDS
                for target in targets
            ):
                writers.add(path.relative_to(PROJECT_ROOT).as_posix())

    assert writers == {"app/services/prepaid_enforcement_state.py"}


def test_prepaid_timer_participant_is_not_called_by_adapters() -> None:
    adapter_callers: set[str] = set()
    for root in (APP_ROOT / "api", APP_ROOT / "web", APP_ROOT / "tasks"):
        for path in root.rglob("*.py"):
            for node in ast.walk(_tree(path)):
                if not isinstance(node, ast.Call):
                    continue
                name = (
                    node.func.id
                    if isinstance(node.func, ast.Name)
                    else node.func.attr
                    if isinstance(node.func, ast.Attribute)
                    else None
                )
                if name in OWNER_FUNCTIONS:
                    adapter_callers.add(path.relative_to(PROJECT_ROOT).as_posix())

    assert adapter_callers == set()


def test_prepaid_timer_participant_only_flushes_and_stages_events() -> None:
    source = OWNER.read_text(encoding="utf-8")

    assert ".commit(" not in source
    assert ".rollback(" not in source
    assert "HTTPException" not in source
    assert "execute_owner_command" not in source
    assert "emit_event(" in source
    assert ".with_for_update()" in source
