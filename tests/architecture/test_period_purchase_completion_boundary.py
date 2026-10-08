"""Guard manual posting and separation of clock evidence from coordinators."""

import ast
from pathlib import Path


def test_outage_posting_only_has_one_staff_approval_call_site():
    tree = ast.parse(
        Path("app/services/outage_compensation.py").read_text(encoding="utf-8")
    )
    callers = []
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef):
            for call in ast.walk(node):
                if (
                    isinstance(call, ast.Call)
                    and isinstance(call.func, ast.Name)
                    and call.func.id == "_stage_outage_compensation"
                    and any(item.arg == "approved_by" for item in call.keywords)
                ):
                    callers.append(node.name)
    assert callers == ["approve_outage_compensation", "operation"]


def test_clock_evidence_has_no_coordinator_imports_or_commits():
    tree = ast.parse(
        Path("app/services/compensated_service_time.py").read_text(encoding="utf-8")
    )
    forbidden = {
        "app.services.account_lifecycle",
        "app.services.outage_compensation",
        "app.services.service_extensions",
    }
    assert not any(
        isinstance(node, ast.ImportFrom) and node.module in forbidden
        for node in ast.walk(tree)
    )
    assert not any(
        isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr in {"commit", "rollback", "begin_nested"}
        for node in ast.walk(tree)
    )


def test_extension_locks_account_before_subscription():
    tree = ast.parse(
        Path("app/services/service_extensions.py").read_text(encoding="utf-8")
    )
    iterator = next(
        node
        for node in tree.body
        if isinstance(node, ast.FunctionDef)
        and node.name == "_iter_scope_subscriptions"
    )
    calls = [node for node in ast.walk(iterator) if isinstance(node, ast.Call)]
    assert not any(
        isinstance(node.func, ast.Attribute) and node.func.attr == "with_for_update"
        for node in calls
    )
    account = next(
        node.lineno
        for node in calls
        if isinstance(node.func, ast.Name) and node.func.id == "lock_account"
    )
    refresh = next(
        node.lineno
        for node in calls
        if isinstance(node.func, ast.Attribute) and node.func.attr == "refresh"
    )
    assert account < refresh
