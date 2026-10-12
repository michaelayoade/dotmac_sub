"""Completion acceptance is evidence authority, never downstream lifecycle authority."""

import ast
from pathlib import Path

from app.services.sot_registry.registry import all_services, registry_validation_errors

ROOT = Path(__file__).resolve().parents[2]


def test_one_completion_owner_and_acyclic_storage_retention() -> None:
    assert not registry_validation_errors()
    owners = [
        service
        for service in all_services()
        if service.name == "operations.field_completion"
    ]
    assert len(owners) == 1
    assert set(owners[0].owner_modules) == {
        "app.services.field.transitions",
        "app.services.field.completion_review",
        "app.services.field.completion_outage_scope",
        "app.services.field.completion_project_scope",
    }
    assert "issued work-order completion review" in owners[0].owns
    retention = next(
        service
        for service in all_services()
        if service.name == "files.evidence_retention"
    )
    assert "operations.field_completion" not in retention.depends_on
    project = next(
        service
        for service in all_services()
        if service.name == "operations.project_lifecycle"
    )
    assert "operations.field_completion" not in project.depends_on


def test_only_executor_completes_and_no_downstream_business_writes() -> None:
    tree = ast.parse((ROOT / "app/services/field/completion_review.py").read_text())
    calls = [node for node in ast.walk(tree) if isinstance(node, ast.Call)]
    assert (
        sum(
            isinstance(node.func, ast.Name) and node.func.id == "execute_owner_command"
            for node in calls
        )
        == 1
    )
    assert not any(
        isinstance(node.func, ast.Attribute)
        and node.func.attr in {"commit", "rollback", "begin_nested"}
        for node in calls
    )
    imports = {
        node.module for node in ast.walk(tree) if isinstance(node, ast.ImportFrom)
    }
    assert not imports & {
        "fastapi",
        "app.services.vendor_portal_operations",
        "app.services.ticket_lifecycle",
        "app.services.provisioning_managers",
    }
    source = (ROOT / "app/services/file_storage.py").read_text()
    assert "app.models.field_completion_review" not in source
    assert source.index(
        "file = lock_file_for_retention", source.index("    def soft_delete")
    ) < source.index(
        "self._storage_client().delete", source.index("    def soft_delete")
    )


def test_additive_immutable_schema_has_no_acceptance_backfill() -> None:
    source = (ROOT / "alembic/versions/677_field_completion_review.py").read_text()
    for marker in (
        "completion_review_immutable",
        "completion_submission_complete",
        "completion_decision_complete",
        "completion_file_retained",
        "completion_attachment_retained",
        "DEFERRABLE INITIALLY DEFERRED",
    ):
        assert marker in source
    assert "INSERT INTO completion" not in source
    assert "DROP TABLE" not in source
    assert "ON CONFLICT (key) DO NOTHING" in source
