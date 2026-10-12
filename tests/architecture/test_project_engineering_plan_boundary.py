"""Guard approval authority, immutable persistence and deferred issuance."""

import ast
from pathlib import Path

from app.services.sot_manifest import TransactionMode
from app.services.sot_registry.registry import all_services, registry_validation_errors

ROOT = Path(__file__).resolve().parents[2]


def test_one_complete_engineering_owner():
    owners = [
        s for s in all_services() if s.name == "operations.project_engineering_plans"
    ]
    assert len(owners) == 1
    assert owners[0].contract.transaction.mode == TransactionMode.OWNER_MANAGED
    assert not registry_validation_errors()


def test_public_mutations_enter_once_and_helpers_never_complete_transactions():
    tree = ast.parse((ROOT / "app/services/project_engineering_plans.py").read_text())
    for name in ("draft_revision", "publish_policy", "decide"):
        node = next(
            n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == name
        )
        calls = [n for n in ast.walk(node) if isinstance(n, ast.Call)]
        assert (
            sum(
                isinstance(n.func, ast.Name) and n.func.id == "execute_owner_command"
                for n in calls
            )
            == 1
        )
    assert not any(
        isinstance(n, ast.Call)
        and isinstance(n.func, ast.Attribute)
        and n.func.attr in {"commit", "rollback", "begin", "begin_nested"}
        for n in ast.walk(tree)
    )
    imports = {n.module for n in ast.walk(tree) if isinstance(n, ast.ImportFrom)}
    assert not imports & {
        "fastapi",
        "app.services.work_order_commands",
        "app.services.file_storage",
        "app.models.stored_file",
    }


def test_origin_reuses_authoritative_project_before_task_lock():
    source = (ROOT / "app/services/projects.py").read_text()
    node = next(
        n
        for n in ast.parse(source).body
        if isinstance(n, ast.FunctionDef) and n.name == "engineering_plan_origin"
    )
    assert any(
        isinstance(n, ast.Call)
        and isinstance(n.func, ast.Name)
        and n.func.id == "_lock_project_task_scope"
        for n in ast.walk(node)
    )
    snippet = ast.get_source_segment(source, node)
    assert '"backlog", "todo", "in_progress"' in snippet
    assert '"open", "planned", "active"' in snippet
    assert "project.revision" not in snippet


def test_migration_preserves_evidence_without_approval_backfill_or_role_grants():
    migration = (ROOT / "alembic/versions/675_project_engineering_plans.py").read_text()
    for table in (
        "engineering_plan_revisions",
        "engineering_plan_criteria",
        "engineering_plan_decisions",
        "engineering_plan_receipts",
        "engineering_policy_revisions",
    ):
        assert f"{table}_immutable" in migration
    assert "BEFORE UPDATE OR DELETE" in migration
    assert "engineering_criteria_creation_only" in migration
    assert "uq_project_tasks_project_id_id" in migration
    assert "INSERT INTO role_permissions" not in migration
    assert "INSERT INTO system_user_permissions" not in migration
    assert "INSERT INTO engineering_policy_revisions" not in migration
    assert "raise RuntimeError" in migration


def test_transport_gate_reuses_canonical_permission_calculation():
    tree = ast.parse((ROOT / "app/services/auth_dependencies.py").read_text())
    assert any(
        isinstance(n, ast.ImportFrom)
        and n.module == "app.services.permission_authorization"
        for n in tree.body
    )
    assert (
        "_canonical_has_permission(_permission_principal(auth)"
        in (ROOT / "app/services/auth_dependencies.py").read_text()
    )
    source = (ROOT / "app/services/permission_authorization.py").read_text()
    assert "_expand_permission_keys" in source


def test_exact_native_history_pairings_are_declared_in_migration():
    source = (ROOT / "alembic/versions/675_project_engineering_plans.py").read_text()
    for name in (
        "fk_engineering_decision_project_policy",
        "fk_engineering_prior_approval_plan",
        "fk_engineering_receipt_decision_revision",
    ):
        assert name in source


def test_strict_staff_admission_has_separate_honest_contract():
    services = {s.name: s for s in all_services()}
    staff = services["auth.staff_permission_admission"]
    assert staff.contract.transaction.mode == TransactionMode.READ_ONLY
    assert staff.depends_on == ("auth.permission_gate", "auth.erp_staff_access")
    assert services["auth.permission_gate"].contract is None
    assert (
        "auth.staff_permission_admission"
        in services["operations.project_engineering_plans"].depends_on
    )
    inputs = {i.name: i.owner for i in staff.contract.authoritative_inputs}
    assert inputs["canonical active Person"] == "party.registry"
    assert inputs["native vendor membership"] == "auth.vendor_user_provisioning"
    source = (ROOT / "app/services/staff_permission_admission.py").read_text()
    tree = ast.parse(source)
    assert "active_leave_restriction" in source
    assert "FieldVendorUser" in source
    assert "has_permission(auth, db, permission)" in source
    assert "SystemUserPermission" not in source
    assert "Any" not in source and "dict[" not in source
    assert not any(
        isinstance(n, ast.Call)
        and isinstance(n.func, ast.Attribute)
        and n.func.attr
        in {"commit", "rollback", "begin", "begin_nested", "add", "execute"}
        for n in ast.walk(tree)
    )
    assert not any(
        isinstance(n, ast.ImportFrom) and n.module == "fastapi" for n in ast.walk(tree)
    )
    baseline = (
        ROOT / "tests/architecture/sot_manifest_legacy_baseline.txt"
    ).read_text()
    assert "auth.permission_gate" in baseline
