"""Keep the exact customer deletion boundary from regaining split writers."""

import ast
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def _function(path, name):
    tree = ast.parse((ROOT / path).read_text())
    return next(
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.FunctionDef) and node.name == name
    )


def _calls(node):
    return [
        call.func.attr
        if isinstance(call.func, ast.Attribute)
        else call.func.id
        if isinstance(call.func, ast.Name)
        else ""
        for call in ast.walk(node)
        if isinstance(call, ast.Call)
    ]


def test_owner_enters_executor_once_without_transaction_completion():
    function = _function(
        "app/services/customer_account_deletion.py", "delete_customer_account"
    )
    calls = _calls(function)
    assert calls.count("execute_owner_command") == 1
    assert not {"commit", "rollback", "begin_nested"}.intersection(calls)
    source = ast.unparse(function)
    assert source.index("capture_and_lock_customer_keys(db") < source.index(
        "select(Subscriber)"
    )
    assert "with_for_update(nowait=True)" in source
    assert "db.delete(subscriber)" in source
    assert "stage_audit_event" in source


def test_migrated_adapters_never_complete_caller_transactions():
    for path, names in {
        "app/services/web_customer_actions.py": (
            "delete_person_customer",
            "delete_business_customer",
            "bulk_delete_customers",
        ),
        "app/web/admin/customers.py": ("person_delete", "business_delete"),
        "app/api/subscribers.py": ("delete_subscriber",),
    }.items():
        for name in names:
            calls = _calls(_function(path, name))
            assert not {
                "commit",
                "rollback",
                "begin_nested",
                "execute_owner_command",
                "log_audit_event",
            }.intersection(calls), name


def test_auth_participants_are_flush_only_and_exactly_admitted():
    for path, names in {
        "app/services/api_key_administration.py": (
            "capture_and_lock_customer_keys",
            "stage_customer_key_deletion",
        ),
        "app/services/credential_administration_mutation.py": (
            "stage_customer_auth_deletion",
        ),
    }.items():
        for name in names:
            calls = _calls(_function(path, name))
            assert "require_customer_deletion_participant" in calls
            assert not {
                "commit",
                "rollback",
                "begin_nested",
                "execute_owner_command",
            }.intersection(calls)
    source = ast.unparse(
        _function(
            "app/services/customer_account_deletion.py",
            "require_customer_deletion_participant",
        )
    )
    assert (
        "current_command_definition(db) != CUSTOMER_ACCOUNT_DELETION_DEFINITION"
        in source
    )
    assert "current_command_context(db) != context" in source
    assert "context.scope != SCOPE" in source


def test_bulk_adapter_uses_fresh_session_per_valid_item():
    single = ast.unparse(
        _function(
            "app/adapters/customer_account_deletion.py",
            "delete_customer_from_adapter",
        )
    )
    bulk = ast.unparse(
        _function("app/services/web_customer_actions.py", "bulk_delete_customers")
    )
    assert "owner_command_session()" in single
    assert "for item in customer_ids" in bulk
    assert "delete_customer_from_adapter(" in bulk
    assert "Unknown customer type" in bulk
