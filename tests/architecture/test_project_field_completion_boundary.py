"""Project terminal gating shares the canonical accepted-evidence owner."""

import ast
from pathlib import Path

from app.services.sot_registry.registry import registry_validation_errors
from app.services.sot_relationships import service_relationship

ROOT = Path(__file__).resolve().parents[2]


def test_project_completion_keeps_one_outer_owner_and_no_reverse_dependency():
    assert not registry_validation_errors()
    field = service_relationship("operations.field_completion")
    project = service_relationship("operations.project_lifecycle")
    assert "app.services.field.completion_project_scope" in field.owner_modules
    assert "accepted project field scope completion" in field.owns
    assert "operations.field_completion" not in project.depends_on
    source = (ROOT / "app/services/field/completion_project_scope.py").read_text()
    tree = ast.parse(source)
    assert (
        sum(
            isinstance(n, ast.Call)
            and isinstance(n.func, ast.Name)
            and n.func.id == "execute_owner_command"
            for n in ast.walk(tree)
        )
        == 1
    )
    assert (
        "stage_scoped_completion_basis" in source
        and "stage_complete_from_accepted_field_scope" in source
    )
    assert not any(
        isinstance(n, ast.Call)
        and isinstance(n.func, ast.Attribute)
        and n.func.attr in {"commit", "rollback", "begin_nested"}
        for n in ast.walk(tree)
    )
    projectsource = (ROOT / "app/services/projects.py").read_text()
    assert "completion_review" not in projectsource
    projection = (
        ROOT / "app/services/project_field_completion_projection.py"
    ).read_text()
    assert "sqlalchemy" not in projection and "app.models" not in projection


def test_project_migration_contract_is_frozen_and_forward_only():
    source = (
        ROOT / "alembic/versions/682_accepted_project_field_completion.py"
    ).read_text()
    guard = (
        ROOT / "scripts/migrations/project_completion_schema_contract.py"
    ).read_text()
    assert "app.models" not in guard and "Base.metadata" not in guard
    assert "681_shared_outage_completion" in source
    assert "FOR UPDATE NOWAIT" in source
    assert "DEFERRABLE INITIALLY DEFERRED" in source
    assert "DROP TABLE" not in source
