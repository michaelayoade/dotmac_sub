"""Keep the explicit coordinator and canonical participants as the only writers."""

import ast
from pathlib import Path

from app.services.sot_registry.registry import all_services, registry_validation_errors

ROOT = Path(__file__).resolve().parents[2]


def test_registered_complete_owner_and_single_executor() -> None:
    matches = [
        s for s in all_services() if s.name == "operations.project_work_order_handoff"
    ]
    assert len(matches) == 1 and matches[0].contract is not None
    assert not registry_validation_errors()
    source = (ROOT / "app/services/project_work_order_handoff.py").read_text()
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
    assert "WorkOrderCommands.stage_native_create" in source
    assert (
        "stage_current_for_issuance" in source and "engineering_plan_origin" in source
    )
    assert not any(
        isinstance(n, ast.Call)
        and isinstance(n.func, ast.Attribute)
        and n.func.attr in {"commit", "rollback", "begin_nested"}
        for n in ast.walk(tree)
    )
    assert not any(
        isinstance(n, ast.Call)
        and isinstance(n.func, ast.Name)
        and n.func.id in {"WorkOrder", "Subscriber", "Ticket"}
        for n in ast.walk(tree)
    )


def test_approval_dispatch_participant_hardwires_permission_and_read_stays_read() -> (
    None
):
    tree = ast.parse((ROOT / "app/services/project_engineering_plans.py").read_text())
    functions = {n.name: n for n in tree.body if isinstance(n, ast.FunctionDef)}
    public = ast.unparse(functions["resolve_current"])
    participant = ast.unparse(functions["stage_current_for_issuance"])
    assert "permission=READ" in public
    assert "operations:dispatch:write" in participant
    assert "IssuanceEngineeringPlanQuery" in participant
    assert "execute_owner_command" not in participant


def test_project_creation_keeps_customer_and_outage_boundaries() -> None:
    source = (ROOT / "app/services/work_order_commands.py").read_text()
    tree = ast.parse(source)
    cls = next(
        n
        for n in tree.body
        if isinstance(n, ast.ClassDef) and n.name == "WorkOrderCommands"
    )
    functions = {n.name: n for n in cls.body if isinstance(n, ast.FunctionDef)}
    project = ast.unparse(functions["_stage_project_native_create"])
    assert "subscriber_id=None" in project and "origin_ticket_id=None" in project
    assert "recommend_template" in project
    assert "notify" not in project and "_queue" not in project
    outage = functions["stage_infrastructure_work_order"]
    assert "origin_ticket_id" in [arg.arg for arg in outage.args.kwonlyargs]
    assert (
        outage.args.kw_defaults[
            [arg.arg for arg in outage.args.kwonlyargs].index("origin_ticket_id")
        ]
        is None
    )
    assert "validate_subscriber_target" in ast.unparse(functions["_create_native"])


def test_issuance_migration_is_additive_immutable_and_preserves_exact_pairing() -> None:
    source = (ROOT / "alembic/versions/676_project_work_order_issuance.py").read_text()
    for required in (
        "fk_project_issuance_approval",
        "fk_project_issuance_plan",
        "fk_project_issuance_task",
        "BEFORE UPDATE OR DELETE",
        "BEFORE INSERT",
        "forward-fix",
    ):
        assert required in source
    assert "DROP TABLE" not in source and "INSERT INTO" not in source
