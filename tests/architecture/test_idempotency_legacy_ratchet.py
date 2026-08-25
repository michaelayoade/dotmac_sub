"""Freeze references to Sub's two active local idempotency owners.

Migration 556 supplies inert kernel-shaped storage for composed modules. It is
not a writer cutover: ``IdempotencyKey`` and ``TaskExecution`` remain the
authoritative local mechanisms and ``idempotent_task`` remains their task
adapter. Transitional coexistence may not silently grow new legacy callers.

The inventory is two-directional: growth is new debt, while shrinkage must
lower the checked-in baseline in the same reviewed retirement slice.
"""

from __future__ import annotations

import ast
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
APP_ROOT = REPO_ROOT / "app"
BASELINE = Path(__file__).with_name("idempotency_legacy_callers.txt")
LEGACY_FROM_IMPORTS = {
    "app.models.idempotency": frozenset({"IdempotencyKey"}),
    "app.models.task_execution": frozenset({"TaskExecution"}),
    "app.services.task_idempotency": frozenset({"idempotent_task"}),
    # app.models is an established public re-export for both model owners.
    "app.models": frozenset({"IdempotencyKey", "TaskExecution"}),
}
LEGACY_MODULE_IMPORTS = frozenset(
    {
        "app.models.idempotency",
        "app.models.task_execution",
        "app.services.task_idempotency",
    }
)
OWNER_FILES = frozenset(
    {
        "app/models/idempotency.py",
        "app/models/task_execution.py",
        "app/services/task_idempotency.py",
    }
)


def _dotted_name(node: ast.expr) -> str | None:
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        prefix = _dotted_name(node.value)
        return f"{prefix}.{node.attr}" if prefix else None
    return None


def _references_legacy_owner(source: str, *, relative_path: str = "") -> bool:
    """Recognise the owned symbols by provenance, never spelling alone."""
    if relative_path in OWNER_FILES:
        return True
    tree = ast.parse(source)
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module in LEGACY_FROM_IMPORTS:
            admitted = LEGACY_FROM_IMPORTS[node.module]
            if any(alias.name in admitted for alias in node.names):
                return True
        if isinstance(node, ast.Import) and any(
            alias.name in LEGACY_MODULE_IMPORTS for alias in node.names
        ):
            return True

    # ``app.models`` re-exports both model owners. A module import alone is not
    # a caller, but dereferencing either owned attribute through its actual
    # bound name is. This closes both ``import app.models`` and aliased-import
    # bypasses without freezing every unrelated aggregate-model import.
    attributes = {
        dotted
        for node in ast.walk(tree)
        if isinstance(node, ast.Attribute)
        if (dotted := _dotted_name(node)) is not None
    }
    for node in ast.walk(tree):
        if not isinstance(node, ast.Import):
            continue
        for alias in node.names:
            if alias.name != "app.models":
                continue
            prefix = alias.asname or "app.models"
            if any(
                f"{prefix}.{symbol}" in attributes
                for symbol in LEGACY_FROM_IMPORTS["app.models"]
            ):
                return True
    return False


def _observed_callers(root: Path = APP_ROOT) -> set[str]:
    observed: set[str] = set()
    for path in root.rglob("*.py"):
        relative = path.relative_to(REPO_ROOT).as_posix()
        if _references_legacy_owner(
            path.read_text(encoding="utf-8"), relative_path=relative
        ):
            observed.add(relative)
    return observed


def _recorded_callers() -> set[str]:
    lines = [
        line.strip()
        for line in BASELINE.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    assert lines == sorted(set(lines)), (
        "legacy-caller baseline must be sorted and unique"
    )
    return set(lines)


def test_legacy_idempotency_callers_are_a_two_directional_ratchet() -> None:
    observed = _observed_callers()
    recorded = _recorded_callers()
    assert observed == recorded, (
        "Sub's active IdempotencyKey/TaskExecution/idempotent_task reference set "
        f"moved: new={sorted(observed - recorded)}, "
        f"retired={sorted(recorded - observed)}. New callers are forbidden. "
        "When a caller is migrated, remove its row from "
        "idempotency_legacy_callers.txt in the same change."
    )


def test_legacy_idempotency_detector_is_red_sensitive_and_exact() -> None:
    assert _references_legacy_owner(
        "from app.models.idempotency import IdempotencyKey\nIdempotencyKey()\n"
    )
    assert _references_legacy_owner(
        "from app.models.task_execution import TaskExecution\nTaskExecution()\n"
    )
    assert _references_legacy_owner(
        "from app.models import IdempotencyKey\nIdempotencyKey()\n"
    )
    assert _references_legacy_owner(
        "from app.models import TaskExecution\nTaskExecution()\n"
    )
    assert _references_legacy_owner("import app.models\napp.models.IdempotencyKey()\n")
    assert _references_legacy_owner(
        "import app.models as models\nmodels.TaskExecution()\n"
    )
    assert _references_legacy_owner(
        "from app.services.task_idempotency import idempotent_task\n"
        "@idempotent_task()\ndef run():\n    pass\n"
    )
    assert not _references_legacy_owner("import app.models\napp.models.Customer()\n")
    assert not _references_legacy_owner(
        "from typing import Annotated\n"
        "IdempotencyKey = Annotated[str, 'header alias']\n"
        "class TaskExecutionStatus:\n    pass\n"
        "def some_idempotent_task():\n    pass\n"
    )
    assert not _references_legacy_owner(
        (APP_ROOT / "api" / "staff_sync.py").read_text(encoding="utf-8"),
        relative_path="app/api/staff_sync.py",
    )
