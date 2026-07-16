"""Architecture guards for the network operation recovery owner."""

from __future__ import annotations

import ast
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[2]
APP_DIR = PROJECT_ROOT / "app"
OWNER = Path("app/services/network_operations.py")
MODEL = Path("app/models/network_operation.py")
REDRIVE_FIELDS = {
    "redrive_of_id",
    "redrive_reason",
    "redrive_reviewed_head",
    "redrive_idempotency_key",
}


def _attribute_name(target: ast.expr) -> str | None:
    return target.attr if isinstance(target, ast.Attribute) else None


def test_redrive_evidence_has_one_application_writer() -> None:
    violations: list[str] = []
    for path in sorted(APP_DIR.rglob("*.py")):
        relative = path.relative_to(PROJECT_ROOT)
        if relative in {OWNER, MODEL}:
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(relative))
        for node in ast.walk(tree):
            targets: list[ast.expr] = []
            if isinstance(node, ast.Assign):
                targets = list(node.targets)
            elif isinstance(node, ast.AnnAssign):
                targets = [node.target]
            elif isinstance(node, ast.AugAssign):
                targets = [node.target]
            for target in targets:
                if _attribute_name(target) in REDRIVE_FIELDS:
                    violations.append(f"{relative}:{node.lineno}")

    assert not violations, (
        "Redrive evidence must be written by the ledger owner:\n"
        + "\n".join(violations)
    )


def test_admin_redrive_adapter_has_no_queue_or_model_write_path() -> None:
    path = PROJECT_ROOT / "app/web/admin/network_operations.py"
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    forbidden_calls = {"enqueue_task", "delay", "apply_async", "NetworkOperation"}
    violations: list[str] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        function = node.func
        name = (
            function.id
            if isinstance(function, ast.Name)
            else function.attr
            if isinstance(function, ast.Attribute)
            else None
        )
        if name in forbidden_calls:
            violations.append(f"{name}:{node.lineno}")

    assert not violations, "Admin adapter bypasses recovery owner: " + ", ".join(
        violations
    )
