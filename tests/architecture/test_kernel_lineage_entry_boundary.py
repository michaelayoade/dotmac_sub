"""Source guards for the deliberately disposable Kernel entry participant."""

from __future__ import annotations

import ast
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SOURCE = ROOT / "scripts/migration/kernel_lineage_entry.py"


def test_entry_participant_has_no_transaction_factory_or_history_shortcut() -> None:
    tree = ast.parse(SOURCE.read_text())
    forbidden = {
        "create_engine",
        "connect",
        "begin",
        "begin_nested",
        "begin_transaction",
        "commit",
        "rollback",
        "stamp",
        "install_idempotent_schema_ops",
    }
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            name = (
                node.func.attr
                if isinstance(node.func, ast.Attribute)
                else node.func.id
                if isinstance(node.func, ast.Name)
                else ""
            )
            assert name not in forbidden, name
    source = SOURCE.read_text()
    assert "environment.run_migrations()" in source
    assert "connection=connection" in source
    assert "version_table=KERNEL_VERSION_TABLE" in source
    assert "script._upgrade_revs" in source
    assert "__main__" not in source
    assert "INSERT INTO" not in source and "UPDATE alembic_version" not in source


def test_real_artifact_and_complete_collision_inventory_remain_explicit() -> None:
    tree = ast.parse(SOURCE.read_text())
    values: dict[str, ast.expr] = {}
    for node in tree.body:
        if isinstance(node, ast.Assign):
            for target in node.targets:
                if isinstance(target, ast.Name):
                    values[target.id] = node.value
        elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
            assert node.value is not None
            values[node.target.id] = node.value
    collisions = ast.literal_eval(values["COLLISION_TABLES"])
    signatures = ast.literal_eval(values["RELAY_SIGNATURES"])
    blobs = ast.literal_eval(values["KERNEL_MIGRATION_BLOBS"])
    assert len(collisions) == 15 and len(set(collisions)) == 15
    assert "party_roles" in collisions
    assert "alembic_version" not in collisions
    assert len(signatures) == 4
    assert len(blobs) == 28
    assert len({name for name, _ in blobs}) == 28
    assert all(len(sha) == 40 for _, sha in blobs)
    assert ast.literal_eval(values["KERNEL_VERSION"]) == "0.1.0a105"
    assert ast.literal_eval(values["KERNEL_HEAD"]) == "0028_machine_attribution"
    # Preserve the old diagnostic rather than converting expected failure into
    # a success/adoption assertion for this much narrower probe.
    legacy = (ROOT / "tests/integration/test_kernel_lineage_rehearsal.py").read_text()
    assert 'EXPECTED_FIRST_FAILURE = "0001_initial_tenant_schema"' in legacy


def test_entry_is_not_composed_or_called_by_runtime_or_deployment() -> None:
    for path in (ROOT / "app").rglob("*.py"):
        assert "kernel_lineage_entry" not in path.read_text(), str(path)
    assert "kernel_lineage_entry" not in (ROOT / "alembic.ini").read_text()
    assert "dotmac_kernel.migrations:versions" not in (ROOT / "alembic.ini").read_text()
    workflow_root = ROOT / ".github/workflows"
    for path in workflow_root.glob("*.yml"):
        source = path.read_text()
        assert "python -m scripts.migration.kernel_lineage_entry" not in source


def test_entry_docs_retain_full_handoff_gates() -> None:
    source = (ROOT / "docs/designs/KERNEL_LINEAGE_ENTRY_PROTOTYPE.md").read_text()
    for claim in (
        "entry-only",
        "not adoption",
        "caller-owned",
        "rollback",
        "canonical Party import",
        "post-Kernel adoption lineage",
        "runtime writer cutover",
        "lost commit acknowledgment",
        "independent post-commit",
        "0.1.0a105",
        "0028_machine_attribution",
    ):
        assert claim in source, claim
