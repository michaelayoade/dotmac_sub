"""Static contract for Sub's ``idempotency_ledger.v1`` provider slice."""

from __future__ import annotations

import ast
from pathlib import Path

from dotmac_kernel.prerequisites import IDEMPOTENCY_LEDGER_V1

from app.migration_bindings import ASSEMBLY_PREREQUISITE_BINDINGS

REPO_ROOT = Path(__file__).resolve().parents[2]
MIGRATION = REPO_ROOT / "alembic/versions/556_idempotency_ledger_prereq.py"


def _literal_assignment(path: Path, name: str):
    for node in ast.parse(path.read_text(encoding="utf-8")).body:
        value: ast.expr | None
        if isinstance(node, ast.Assign):
            targets = node.targets
            value = node.value
        elif isinstance(node, ast.AnnAssign):
            targets = [node.target]
            value = node.value
        else:
            continue
        if any(
            isinstance(target, ast.Name) and target.id == name for target in targets
        ):
            assert value is not None
            return ast.literal_eval(value)
    raise AssertionError(f"{path.name} declares no literal {name}")


def test_provider_revision_extends_subs_current_product_head() -> None:
    assert _literal_assignment(MIGRATION, "revision") == (
        "556_idempotency_ledger_prereq"
    )
    assert _literal_assignment(MIGRATION, "down_revision") == (
        "555_cx_handoff_permissions"
    )
    assert _literal_assignment(MIGRATION, "REQUIRES") == (IDEMPOTENCY_LEDGER_V1.name,)


def test_binding_names_subs_provider_revision_without_running_kernel_lineage() -> None:
    binding = next(
        candidate
        for candidate in ASSEMBLY_PREREQUISITE_BINDINGS
        if candidate.prerequisite == IDEMPOTENCY_LEDGER_V1.name
    )
    assert binding.provider_revision == "556_idempotency_ledger_prereq"
    assert binding.provider_owner == "sub"


def test_migration_hosts_both_exact_ledger_planes() -> None:
    source = MIGRATION.read_text(encoding="utf-8")
    for table in ("idempotency_records", "platform_idempotency_records"):
        assert f'"{table}"' in source
        assert f"ix_{table}_expires_at" in source
    for column in (
        "id",
        "scope",
        "key",
        "fingerprint",
        "operation",
        "status",
        "result",
        "correlation_id",
        "expires_at",
        "created_at",
        "updated_at",
    ):
        assert f'"{column}"' in source
    assert "uq_idempotency_records_tenant_scope_key" in source
    assert "uq_platform_idempotency_records_scope_key" in source
    assert "fk_idempotency_records_tenant" in source


def test_migration_declares_the_two_plane_isolation_posture() -> None:
    source = MIGRATION.read_text(encoding="utf-8")
    assert "public.idempotency_records ENABLE ROW LEVEL SECURITY" in source
    assert "public.idempotency_records FORCE ROW LEVEL SECURITY" in source
    assert "CREATE POLICY idempotency_records_tenant_isolation" in source
    assert "tenant_id = public.app_current_tenant_id()" in source
    assert "platform_idempotency_records ENABLE ROW LEVEL SECURITY" not in source
    assert "platform_idempotency_records FORCE ROW LEVEL SECURITY" not in source
    assert (
        "REVOKE ALL PRIVILEGES ON TABLE public.platform_idempotency_records" in source
    )
    assert 'FROM app_user"' in source
    assert "GRANT SELECT, INSERT, UPDATE, DELETE ON TABLE" in source


def test_upgrade_closes_with_the_pinned_live_verifier() -> None:
    tree = ast.parse(MIGRATION.read_text(encoding="utf-8"))
    upgrade = next(
        node
        for node in tree.body
        if isinstance(node, ast.FunctionDef) and node.name == "upgrade"
    )
    final = upgrade.body[-1]
    assert isinstance(final, ast.Expr)
    assert isinstance(final.value, ast.Call)
    assert isinstance(final.value.func, ast.Name)
    assert final.value.func.id == "require_prerequisites"


def test_provider_does_not_move_a_legacy_caller_or_run_kernel_revisions() -> None:
    source = MIGRATION.read_text(encoding="utf-8")
    assert "INSERT INTO idempotency_keys" not in source
    assert "INSERT INTO task_executions" not in source
    assert "stamp(" not in source
    assert "0001_initial_tenant_schema" not in source
    assert "dotmac_kernel.migrations.versions" not in source
