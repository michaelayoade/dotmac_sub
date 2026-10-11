"""Prevent the bounded delivery adapters returning to request-time SMTP."""

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
    return {
        call.func.attr if isinstance(call.func, ast.Attribute) else call.func.id
        for call in ast.walk(node)
        if isinstance(call, ast.Call)
        and isinstance(call.func, (ast.Name, ast.Attribute))
    }


def test_resend_adapters_use_fresh_owner_sessions_and_never_release_caller():
    for path, name in (
        ("app/api/auth_flow.py", "resend_verification_email_endpoint"),
        ("app/web/customer/routes.py", "customer_resend_email_verification"),
        ("app/services/web_reseller_routes.py", "reseller_resend_email_verification"),
    ):
        calls = _calls(_function(path, name))
        assert {"owner_command_session", "request_email_verification"} <= calls
        assert (
            not {
                "release_read_transaction",
                "rollback",
                "commit",
                "send_email_verification",
            }
            & calls
        )


def test_request_participant_never_mints_renders_or_completes_transaction():
    calls = _calls(
        _function("app/services/email_verification.py", "stage_email_verification")
    )
    assert {"stage_audit_event", "submit", "flush"} <= calls
    assert (
        not {
            "commit",
            "rollback",
            "_issue_email_verification_token",
            "send_email",
            "render_email_verification_email",
        }
        & calls
    )


def test_portal_profile_stages_verification_inside_owner_operation():
    node = _function("app/services/customer_portal_profile_commands.py", "operation")
    assert "stage_email_verification" in _calls(node)
    source = (ROOT / "app/services/customer_portal_profile_commands.py").read_text()
    assert "send_email_verification(" not in source


def test_legacy_direct_verification_has_only_the_mobile_compatibility_consumer():
    consumers = set()
    materializers = set()
    for path in (ROOT / "app").rglob("*.py"):
        tree = ast.parse(path.read_text())
        for node in ast.walk(tree):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                for call in ast.walk(node):
                    if not isinstance(call, ast.Call):
                        continue
                    name = (
                        call.func.attr
                        if isinstance(call.func, ast.Attribute)
                        else call.func.id
                        if isinstance(call.func, ast.Name)
                        else ""
                    )
                    if name == "send_email_verification":
                        consumers.add((str(path.relative_to(ROOT)), node.name))
                    if name == "materialize_verification_email":
                        materializers.add((str(path.relative_to(ROOT)), node.name))
    assert consumers == {("app/services/auth_flow.py", "set_subscriber_email")}
    assert materializers == {
        ("app/services/ephemeral_communication_actions.py", "materialize_email")
    }


def test_retired_profile_adapter_and_alias_fallbacks_cannot_return():
    retired = {
        "update_customer_profile",
        "UpdateCustomerProfileCommand",
        "CustomerProfileUpdateOutcome",
    }
    legacy = ROOT / "app/services/web_customer_actions.py"
    definitions = {
        node.name
        for node in ast.parse(legacy.read_text()).body
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef))
    }
    assert not retired & definitions
    for directory in (ROOT / "app", ROOT / "scripts"):
        for path in directory.rglob("*.py"):
            tree = ast.parse(path.read_text())
            aliases = {"web_customer_actions"}
            for node in ast.walk(tree):
                if (
                    isinstance(node, ast.ImportFrom)
                    and node.module == "app.services.web_customer_actions"
                ):
                    assert not {alias.name for alias in node.names} & retired, str(path)
                if isinstance(node, ast.ImportFrom) and node.module == "app.services":
                    aliases.update(
                        alias.asname or alias.name
                        for alias in node.names
                        if alias.name == "web_customer_actions"
                    )
                if isinstance(node, ast.Import):
                    aliases.update(
                        alias.asname or alias.name
                        for alias in node.names
                        if alias.name == "app.services.web_customer_actions"
                    )
            for node in ast.walk(tree):
                if isinstance(node, ast.Attribute) and node.attr in retired:
                    assert ast.unparse(node.value) not in aliases, str(path)
                if (
                    isinstance(node, ast.Call)
                    and isinstance(node.func, ast.Name)
                    and node.func.id == "getattr"
                    and len(node.args) >= 2
                ):
                    assert not (
                        ast.unparse(node.args[0]) in aliases
                        and isinstance(node.args[1], ast.Constant)
                        and node.args[1].value in retired
                    ), str(path)
