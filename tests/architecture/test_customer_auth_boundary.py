"""The customer web and native adapters must share one password-step policy."""

import ast
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def _function(path: str, name: str) -> ast.FunctionDef:
    tree = ast.parse((ROOT / path).read_text(encoding="utf-8"))
    return next(
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.FunctionDef) and node.name == name
    )


def test_both_adapters_call_shared_customer_preparation_once() -> None:
    for path, name in [
        ("app/services/web_customer_auth.py", "customer_login_submit"),
        ("app/api/customer_auth.py", "login"),
    ]:
        function = _function(path, name)
        calls = [
            ast.unparse(node.func)
            for node in ast.walk(function)
            if isinstance(node, ast.Call)
        ]
        assert calls.count("customer_authentication.prepare_customer_login") == 1
        assert not any(
            call.endswith("verify_user_password")
            or call.endswith("radius_auth.authenticate")
            or call.endswith("resolve_customer_login_identity")
            for call in calls
        )


def test_shared_policy_has_no_http_imports_or_transaction_completion() -> None:
    tree = ast.parse((ROOT / "app/services/customer_authentication.py").read_text())
    assert not any(
        isinstance(node, ast.ImportFrom)
        and node.module
        and node.module.startswith(("fastapi", "starlette"))
        for node in ast.walk(tree)
    )
    assert not any(
        isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr in {"commit", "rollback"}
        for node in ast.walk(tree)
    )


def test_customer_web_adapter_has_no_retired_password_policy_helpers() -> None:
    tree = ast.parse((ROOT / "app/services/web_customer_auth.py").read_text())
    names = {node.name for node in tree.body if isinstance(node, ast.FunctionDef)}
    assert not names.intersection(
        {"_record_customer_local_login_failure", "_primary_customer_totp_enabled"}
    )


def test_customer_mfa_has_explicit_customer_audience() -> None:
    function = _function("app/api/customer_auth.py", "verify_mfa")
    calls = [
        node
        for node in ast.walk(function)
        if isinstance(node, ast.Call)
        and ast.unparse(node.func) == "auth_flow.AuthFlow.mfa_verify"
    ]
    assert len(calls) == 1
    assert any(
        keyword.arg == "audience"
        and ast.unparse(keyword.value) == "auth_flow.LoginAudience.customer"
        for keyword in calls[0].keywords
    )
