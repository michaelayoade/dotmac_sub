"""Support owns consent; completion owns exact enrolled acceptance."""

import ast
from pathlib import Path

from app.services.sot_registry.registry import registry_validation_errors

ROOT = Path(__file__).resolve().parents[2]


def test_direct_close_uses_canonical_enrollment_presence_without_source_bypass() -> (
    None
):
    tree = ast.parse((ROOT / "app/services/support.py").read_text())
    functions = {
        node.name: node for node in tree.body if isinstance(node, ast.FunctionDef)
    }
    source = ast.unparse(functions["transition_ticket_status"])
    assert "_linked_completion_enrollments" in source
    assert "resolution_confirmation_required" in source
    assert "_write_ticket_status" in source
    assert "source ==" not in source and "source in" not in source
    helper = ast.unparse(functions["_ticket_completion_basis"])
    assert "stage_ticket_completion_basis" in helper
    assert "ProjectTask" not in helper and "query(" not in helper
    assert not registry_validation_errors()


def test_ticket_handoff_fences_before_ticket_lock() -> None:
    source = (ROOT / "app/services/ticket_work_order_handoff.py").read_text()
    assert source.index("lock_work_order_linkage(") < source.index(".with_for_update()")


def test_confirmation_validates_basis_before_private_status_write() -> None:
    tree = ast.parse((ROOT / "app/services/support.py").read_text())
    tickets = next(
        node
        for node in tree.body
        if isinstance(node, ast.ClassDef) and node.name == "Tickets"
    )
    functions = {
        node.name: node for node in tickets.body if isinstance(node, ast.FunctionDef)
    }
    confirm = ast.unparse(functions["confirm_resolution"])
    assert confirm.index("_validate_confirmation_basis(") < confirm.index(
        "_write_ticket_status("
    )
    timer = ast.unparse(functions["consume_resolution_confirmation_due"])
    assert (
        "blocked_completion_basis" in timer
        and "_auto_confirmation_basis_allowed" in timer
    )


def test_migration_preserves_native_exact_lineage_and_immutability() -> None:
    source = (
        ROOT / "alembic/versions/678_support_resolution_completion_basis.py"
    ).read_text()
    for required in (
        "677_field_completion_review",
        "BEFORE UPDATE OR DELETE",
        "BEFORE INSERT",
        "origin_ticket_id",
        "completed_event_id",
        "assignment_generation",
        "review_generation",
        "forward-fix",
        "get_check_constraints",
    ):
        assert required in source
    assert "DROP TABLE" not in source
