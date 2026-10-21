"""Keep followup accountability away from scheduling and customer consequences."""

import ast
from pathlib import Path


def test_followup_owner_has_no_customer_or_job_consequence_writer():
    path = Path("app/services/field/visit_followup.py")
    tree = ast.parse(path.read_text())
    imports = {
        node.module for node in ast.walk(tree) if isinstance(node, ast.ImportFrom)
    }
    assert not imports.intersection(
        {
            "app.services.support",
            "app.services.dispatch",
            "app.services.field.transitions",
            "app.services.team_inbox_field_job",
            "app.services.customer_work_order_selfcare",
        }
    )
    calls = [node for node in ast.walk(tree) if isinstance(node, ast.Call)]
    assert not any(
        isinstance(node.func, ast.Attribute)
        and node.func.attr
        in {
            "commit",
            "rollback",
            "assign",
            "transition",
            "schedule",
            "resolve",
            "create_work_order",
        }
        for node in calls
    )
    events = [
        node
        for node in calls
        if isinstance(node.func, ast.Name) and node.func.id == "emit_event"
    ]
    assert len(events) == 1
    assert any(
        keyword.arg == "record_only"
        and isinstance(keyword.value, ast.Constant)
        and keyword.value.value is True
        for keyword in events[0].keywords
    )


def test_completion_proof_precedes_followup_work_order_lock():
    tree = ast.parse(Path("app/services/field/visit_followup.py").read_text())
    stage = next(
        node
        for node in tree.body
        if isinstance(node, ast.FunctionDef) and node.name == "_stage"
    )
    calls = {
        node.func.id: node.lineno
        for node in ast.walk(stage)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id in {"_validate_replacement", "_work_order"}
    }
    assert calls["_validate_replacement"] < calls["_work_order"]


def test_followup_does_not_rederive_completion_engineering_approval():
    source = Path("app/services/field/visit_followup.py").read_text()
    assert "current_replacement_evidence(" in source
    assert "CompletionReplacementDecision" not in source
    assert "ProjectEngineering" not in source
    assert "replacement_work_order_id ==" not in source
