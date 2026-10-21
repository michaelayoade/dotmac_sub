"""Reviewed supersession remains completion-owned; support stores exact lineage."""

import ast
from pathlib import Path

from app.services.sot_registry.registry import all_services, registry_validation_errors

ROOT = Path(__file__).resolve().parents[2]


def test_replacement_has_one_registered_completion_owner_and_shared_executor() -> None:
    assert not registry_validation_errors()
    owner = next(
        service
        for service in all_services()
        if service.name == "operations.field_completion"
    )
    assert "reviewed failed completion scope replacement" in owner.owns
    assert "failed completion replacement workspace" in owner.owns
    source = (ROOT / "app/services/field/completion_review.py").read_text()
    tree = ast.parse(source)
    functions = {
        node.name: node for node in tree.body if isinstance(node, ast.FunctionDef)
    }
    for name in ("review_failed_scope_replacement", "revoke_failed_scope_replacement"):
        assert "_execute_replacement" in ast.unparse(functions[name])
    assert "_run_owner" in ast.unparse(functions["_execute_replacement"])
    assert (
        sum(
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Name)
            and node.func.id == "execute_owner_command"
            for node in ast.walk(tree)
        )
        == 1
    )
    assert not any(
        isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr in {"commit", "rollback", "begin_nested"}
        for node in ast.walk(tree)
    )
    writes = {
        node.attr
        for node in ast.walk(tree)
        if isinstance(node, ast.Attribute) and isinstance(node.ctx, ast.Store)
    }
    assert not writes & {
        "status",
        "subscriber_id",
        "origin_ticket_id",
        "project_task_id",
    }


def test_migration_adoption_is_frozen_and_retains_required_original_scope() -> None:
    guard = (ROOT / "scripts/migrations/replacement_schema_contract.py").read_text()
    assert "app.models" not in guard and "Base.metadata" not in guard
    assert "Nonempty pre-migration replacement table" in guard
    migration = (
        ROOT / "alembic/versions/679_reviewed_failed_scope_replacement.py"
    ).read_text()
    for invariant in (
        "678_support_resolution_completion_basis",
        "verify_replacement_adoption",
        "completion_replacement_coverage_complete",
        "completion_replacement_head_lineage",
        "support_resolution_original",
        "DEFERRABLE INITIALLY DEFERRED",
        "forward-fix",
    ):
        if invariant == "support_resolution_original":
            invariant = "support resolution original required scope mismatch"
        assert invariant in migration
    assert "INSERT INTO completion" not in migration
    assert "DROP TABLE" not in migration
