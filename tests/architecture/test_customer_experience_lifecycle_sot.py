from __future__ import annotations

import ast
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
APP = ROOT / "app"


def test_customer_lifecycle_projection_is_read_only():
    path = APP / "services/customer_experience_lifecycle.py"
    tree = ast.parse(path.read_text())
    prohibited = {"add", "delete", "commit", "flush", "execute"}
    calls = [
        node.func.attr
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr in prohibited
    ]
    assert calls == []


def test_customer_work_surfaces_use_native_projection_and_selfcare_owner():
    for relative in (
        "api/me.py",
        "web/customer/projects.py",
        "web/customer/work_orders.py",
        "services/reseller_crm_views.py",
    ):
        source = (APP / relative).read_text()
        assert "projects_mirror" not in source
        assert "work_orders_mirror" not in source
    assert (
        "customer_experience_lifecycle.projects_for_subscriber"
        in (APP / "api/me.py").read_text()
    )
    assert (
        "customer_work_order_selfcare.rate_technician"
        in (APP / "api/me.py").read_text()
    )


def test_project_task_relationship_is_guarded_by_work_order_command_owner():
    owner = (APP / "services/work_order_commands.py").read_text()
    assert "validate_project_task_target" in owner
    assert "project_task_binding_immutable" in owner
    assert 'data["project_task_id"] = normalized_task_id' in owner

    constructors = []
    for path in (APP / "services").rglob("*.py"):
        tree = ast.parse(path.read_text())
        if any(
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Name)
            and node.func.id == "WorkOrder"
            for node in ast.walk(tree)
        ):
            constructors.append(path.relative_to(ROOT).as_posix())
    assert constructors == ["app/services/work_order_commands.py"]


def test_retired_work_mirror_runtime_is_absent():
    for relative in (
        "models/project_mirror.py",
        "services/projects_mirror.py",
        "services/work_orders_mirror.py",
        "tasks/projects.py",
        "tasks/work_orders.py",
    ):
        assert not (APP / relative).exists()


def test_crm_connector_cannot_read_project_or_work_order_authority():
    forbidden = (
        "list_work_orders",
        "get_work_order",
        "list_work_order_notes",
        "get_portal_projects",
        "get_portal_work_orders",
        "get_portal_technician_location",
    )
    for relative in (
        "services/crm_client.py",
        "services/integrations/crm_capability.py",
        "services/integrations/connectors/dotmac_crm.py",
    ):
        source = (APP / relative).read_text()
        for operation in forbidden:
            assert operation not in source, f"{relative} exposes {operation}"


def test_assignment_customer_consequence_is_a_typed_flush_only_participant():
    from app.services.sot_manifest import TransactionMode
    from app.services.sot_registry.registry import service_relationship

    contract = service_relationship(
        "communications.work_order_assignment_updates"
    ).contract
    assert contract is not None
    assert contract.transaction.mode is TransactionMode.PARTICIPANT
    assert contract.events is not None
    assert contract.events.delivery_owner == "communications.intents"

    tree = ast.parse(
        (APP / "services/customer_experience_communications.py").read_text()
    )
    participant = next(
        node
        for node in tree.body
        if isinstance(node, ast.FunctionDef)
        and node.name == "request_assignment_update"
    )
    request = next(arg for arg in participant.args.kwonlyargs if arg.arg == "request")
    assert ast.unparse(request.annotation) == "AssignmentCommunicationRequest"
    assert ast.unparse(participant.returns) == "CommunicationIntentResult | None"
    forbidden = {
        "commit",
        "rollback",
        "begin_nested",
        "deliver",
        "send_email",
        "send_whatsapp",
    }
    assert not [
        node
        for node in ast.walk(participant)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr in forbidden
    ]

    owner = ast.parse((APP / "services/work_order_commands.py").read_text())
    assignment = next(
        node
        for node in owner.body
        if isinstance(node, ast.FunctionDef) and node.name == "_assign_work_order"
    )
    assert any(
        isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id == "request_assignment_update"
        for node in ast.walk(assignment)
    )


def test_technician_rating_owner_and_scorecard_provenance_are_contracted():
    from app.services.sot_manifest import TransactionMode
    from app.services.sot_registry.registry import service_relationship

    rating = service_relationship("customer.work_order_selfcare").contract
    assert rating is not None
    assert rating.transaction.mode is TransactionMode.OWNER_MANAGED
    assert rating.events is not None
    scorecards = service_relationship("customer.technician_satisfaction").contract
    assert scorecards is not None
    assert scorecards.transaction.mode is TransactionMode.READ_ONLY
    tree = ast.parse((APP / "services/customer_work_order_selfcare.py").read_text())
    command = next(
        node
        for node in tree.body
        if isinstance(node, ast.FunctionDef) and node.name == "rate_technician"
    )
    calls = [node for node in ast.walk(command) if isinstance(node, ast.Call)]
    assert (
        sum(
            isinstance(node.func, ast.Name) and node.func.id == "execute_owner_command"
            for node in calls
        )
        == 1
    )
    assert not any(
        isinstance(node.func, ast.Attribute)
        and node.func.attr in {"commit", "rollback"}
        for node in calls
    )
    source = (APP / "services/technician_satisfaction.py").read_text()
    assert "recorded_rating_attribution(row)" in source
    assert "WorkOrderAssignmentQueue" not in source
    lifecycle = (APP / "services/customer_experience_lifecycle.py").read_text()
    assert "rating_eligibility(" in lifecycle
    assert "row.assigned_to_name" not in lifecycle
