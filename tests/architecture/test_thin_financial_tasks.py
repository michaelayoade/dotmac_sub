"""Architecture checks for migrated financial Celery wrappers."""

from __future__ import annotations

import re
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[2]
TASK_FILES = (
    PROJECT_ROOT / "app" / "tasks" / "billing.py",
    PROJECT_ROOT / "app" / "tasks" / "collections.py",
    PROJECT_ROOT / "app" / "tasks" / "enforcement.py",
)
DISALLOWED_PATTERNS = (
    re.compile(r"\bfrom app\.models\b"),
    re.compile(r"\bfrom sqlalchemy\b"),
    re.compile(r"\bdb_session_adapter\b"),
    re.compile(r"\bSessionLocal\b"),
    re.compile(r"\.(?:query|execute|commit|rollback)\("),
)


def test_migrated_financial_tasks_remain_thin_wrappers() -> None:
    violations: list[str] = []

    for path in TASK_FILES:
        source = path.read_text(encoding="utf-8")
        for pattern in DISALLOWED_PATTERNS:
            for match in pattern.finditer(source):
                line = source.count("\n", 0, match.start()) + 1
                violations.append(
                    f"{path.relative_to(PROJECT_ROOT)}:{line} -> {pattern.pattern}"
                )

    assert not violations, "\n".join(violations)


def test_payment_reconciliation_task_owns_only_session_and_serialization() -> None:
    source = (PROJECT_ROOT / "app" / "tasks" / "payment_reconciliation.py").read_text(
        encoding="utf-8"
    )

    assert "db_session_adapter.owner_command_session()" in source
    assert "RunTopupReconciliationCommand(" in source
    assert "CommandContext.system(" in source
    assert "reconcile_pending_topups(" in source
    assert ".as_dict()" in source
    assert "SessionLocal" not in source
    assert ".commit(" not in source
    assert ".rollback(" not in source
    assert "from app.models" not in source
