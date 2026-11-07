"""Keep infrastructure choices and issuance behind their registered owners."""

import ast
from pathlib import Path

from app.services.sot_registry.registry import all_services

ROOT = Path(__file__).resolve().parents[2]


def test_selection_and_handoff_have_complete_registered_contracts():
    owners = {service.name: service for service in all_services()}
    selection = owners["network.infrastructure_ticket_selection"]
    handoff = owners["network.outage_work_order_handoff"]
    assert selection.contract is not None
    assert handoff.contract is not None
    assert "network.infrastructure_catalogue" in selection.depends_on
    assert "network.infrastructure_ticket_selection" in handoff.depends_on


def test_web_adapter_cannot_declare_or_link_outages():
    source = (ROOT / "app/web/admin/support_tickets.py").read_text()
    calls = {
        ast.unparse(node.func)
        for node in ast.walk(ast.parse(source))
        if isinstance(node, ast.Call)
    }
    assert "declare_outage" not in calls
    assert "link_infrastructure_ticket" not in calls
    assert "execute_owner_command" not in calls


def test_generic_ticket_create_retains_owner_gate_for_infrastructure():
    source = (ROOT / "app/services/support.py").read_text()
    tree = ast.parse(source)
    tickets = next(
        node
        for node in tree.body
        if isinstance(node, ast.ClassDef) and node.name == "Tickets"
    )
    stage = next(
        node
        for node in tickets.body
        if isinstance(node, ast.FunctionDef) and node.name == "_stage_create"
    )
    body = ast.unparse(stage)
    assert "current_command_definition(db)" in body
    assert "create_infrastructure_manual" in body
    assert "create_infrastructure_ticket" in body


def test_support_create_adapter_passes_typed_form_command():
    source = (ROOT / "app/web/admin/support_tickets.py").read_text()
    tree = ast.parse(source)
    calls = [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and ast.unparse(node.func).endswith("create_ticket_from_form")
    ]
    assert len(calls) == 1
    assert {keyword.arg for keyword in calls[0].keywords} == {
        "command",
        "request",
        "attachments",
    }
