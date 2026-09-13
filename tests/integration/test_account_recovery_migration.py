"""Structural checks on the two account_recovery migrations.

These do NOT apply the migrations against a real database (author-only per
the task's authority limits). Running the actual upgrade/downgrade rehearsal
against disposable Postgres is CI's job. This module instead verifies the
static properties that must hold before that rehearsal can even be
attempted: correct revision chaining, and that downgrade refuses rather than
silently dropping tombstones once rows exist.
"""

from __future__ import annotations

import ast
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
VERSIONS = ROOT / "alembic" / "versions"
EVIDENCE = VERSIONS / "607_account_recovery_evidence.py"
PERMISSIONS = VERSIONS / "608_account_recovery_permissions.py"


def _module_vars(path: Path) -> dict[str, object]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    values: dict[str, object] = {}
    for node in tree.body:
        targets = []
        value = None
        if isinstance(node, ast.Assign):
            targets, value = node.targets, node.value
        elif isinstance(node, ast.AnnAssign):
            targets, value = [node.target], node.value
        for t in targets:
            if isinstance(t, ast.Name) and value is not None:
                try:
                    values[t.id] = ast.literal_eval(value)
                except Exception:
                    pass
    return values


def test_607_chains_off_the_verified_head() -> None:
    values = _module_vars(EVIDENCE)
    assert values["revision"] == "607_account_recovery_evidence"
    assert values["down_revision"] == "606_project_task_subtasks"


def test_608_chains_off_607() -> None:
    values = _module_vars(PERMISSIONS)
    assert values["revision"] == "608_account_recovery_permissions"
    assert values["down_revision"] == "607_account_recovery_evidence"


def test_no_other_migration_also_claims_606_as_its_parent() -> None:
    """Exactly one child of 606 — otherwise alembic has two heads again."""
    claimants = []
    for path in VERSIONS.glob("*.py"):
        values = _module_vars(path)
        if values.get("down_revision") == "606_project_task_subtasks":
            claimants.append(path.name)
    assert claimants == ["607_account_recovery_evidence.py"], claimants


def test_evidence_migration_downgrade_refuses_once_rows_exist() -> None:
    source = EVIDENCE.read_text(encoding="utf-8")
    downgrade = source[source.index("def downgrade"):]
    assert "raise RuntimeError" in downgrade
    assert "count" in downgrade


def test_evidence_migration_backfills_before_removing_legacy_keys() -> None:
    source = EVIDENCE.read_text(encoding="utf-8")
    backfill_index = source.index("INSERT INTO account_recovery_records")
    pop_index = source.index('cleaned.pop(')
    assert backfill_index < pop_index, (
        "legacy metadata_ keys must only be removed after the typed row exists"
    )


def test_evidence_migration_never_narrows_tool_lineage_to_subscription_only() -> None:
    """Fail-closed: the retired cascade tool's rows must always include the
    non-subscription categories it could have touched, since the JSON
    snapshot never recorded invoice/payment/RADIUS/IP/ONT/splitter
    involvement at all."""
    source = EVIDENCE.read_text(encoding="utf-8")
    assert "_CASCADE_ALWAYS_AFFECTED" in source
    tool_branch = source[
        source.index("if has_tool_lineage:") : source.index("elif has_self_service_lineage:")
    ]
    assert "affected.update(_CASCADE_ALWAYS_AFFECTED)" in tool_branch
