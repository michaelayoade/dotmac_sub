"""Guards for migrations that follow the model-based squashed schema."""

from __future__ import annotations

import ast
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[2]
MIGRATIONS_DIR = PROJECT_ROOT / "alembic" / "versions"
BATCH_OPERATION_ALLOWLIST = {
    # This historical schema-removal migration inspects every affected table
    # and column before entering its batch operations.
    "162_drop_olt_circuit_breaker_schema.py",
}


def test_post_squash_migrations_use_guarded_top_level_operations() -> None:
    violations: list[str] = []
    for path in sorted(MIGRATIONS_DIR.glob("*.py")):
        if path.name == "001_squashed_initial_schema.py" or (
            path.name in BATCH_OPERATION_ALLOWLIST
        ):
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Attribute)
                and node.func.attr == "batch_alter_table"
            ):
                violations.append(f"{path.relative_to(PROJECT_ROOT)}:{node.lineno}")

    assert not violations, (
        "Post-squash migrations must use top-level Alembic operations so "
        "alembic/env.py can make fresh-schema changes idempotent:\n"
        + "\n".join(violations)
    )
