"""Prevent counter-only failures and request-session transaction ownership."""

from __future__ import annotations

import ast
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def _function(path: str, name: str) -> ast.FunctionDef:
    tree = ast.parse((ROOT / path).read_text())
    return next(
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.FunctionDef) and node.name == name
    )


def test_failure_owner_uses_one_fresh_session_and_one_executor():
    function = _function(
        "app/services/password_authentication.py", "record_authentication_denial"
    )
    calls = [node for node in ast.walk(function) if isinstance(node, ast.Call)]
    names = [
        node.func.id
        if isinstance(node.func, ast.Name)
        else node.func.attr
        if isinstance(node.func, ast.Attribute)
        else ""
        for node in calls
    ]
    assert names.count("owner_command_session") == 1
    assert names.count("execute_owner_command") == 1
    assert names.count("stage_audit_event") == 1
    assert not {
        "release_read_transaction",
        "get_bind",
        "commit",
        "rollback",
        "record_audit_event",
    }.intersection(names)
    assert all(keyword.arg != "exc_info" for node in calls for keyword in node.keywords)
    assert [arg.arg for arg in function.args.args] == ["denial"]


def test_mfa_success_owner_cannot_dispatch_nested_failure():
    function = _function("app/services/password_authentication.py", "complete_mfa")
    calls = {
        node.func.id
        for node in ast.walk(function)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
    }
    assert not {"record_mfa_failure", "record_authentication_denial"}.intersection(
        calls
    )


def test_enrollment_legacy_failure_helper_has_no_transaction_completion():
    function = _function("app/services/auth_flow.py", "record_mfa_failure")
    attributes = {
        node.func.attr
        for node in ast.walk(function)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
    }
    assert "record_mfa_failure" in attributes
    assert not {"commit", "rollback", "execute"}.intersection(attributes)


def test_denial_contract_contains_no_request_session_or_secret_fields():
    from dataclasses import fields

    from app.services.password_authentication import AuthenticationDenial, DenialTarget

    assert {item.name for item in fields(AuthenticationDenial)} == {
        "reason",
        "surface",
        "phase",
        "target",
        "attempt_id",
        "correlation_id",
        "trusted_ip",
        "effect",
        "status_code",
    }
    assert {item.name for item in fields(DenialTarget)} == {
        "kind",
        "identifier",
        "provenance",
    }
