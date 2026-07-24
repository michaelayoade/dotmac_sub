from pathlib import Path

from app.services.sot_manifest import TransactionMode
from app.services.sot_relationships import service_relationship

ROOT = Path(__file__).resolve().parents[2]


def test_projects_owners_have_complete_typed_contracts() -> None:
    lifecycle = service_relationship("operations.project_lifecycle")
    assignment = service_relationship("operations.project_assignment_policy")
    projection = service_relationship("ui.project_list_projection")

    assert lifecycle.contract is not None
    assert lifecycle.contract.transaction.mode is TransactionMode.OWNER_MANAGED
    assert assignment.contract is not None
    assert assignment.contract.transaction.mode is TransactionMode.READ_ONLY
    assert projection.contract is not None
    assert projection.contract.transaction.mode is TransactionMode.READ_ONLY


def test_ticket_assignment_engine_does_not_write_project_state() -> None:
    source = (ROOT / "app/services/ticket_assignment/engine.py").read_text()
    forbidden = (
        "project.manager_person_id =",
        "project.project_manager_person_id =",
        "project.assistant_manager_person_id =",
        "project.service_team_id =",
        "task.assigned_to_person_id =",
        "task.assignees.append(",
    )
    assert not [token for token in forbidden if token in source]


def test_project_adapters_do_not_complete_transactions() -> None:
    for relative in ("app/api/projects.py", "app/web/admin/projects.py"):
        source = (ROOT / relative).read_text()
        assert ".commit(" not in source
