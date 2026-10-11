"""User authentication and service-secret verification stay separate."""

from __future__ import annotations

import ast
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
RETIRED_VERIFIER = "verify_" + "password"
USER_MODULES = (
    "auth_flow",
    "web_customer_auth",
    "web_system_profiles",
    "web_system_db_inspector",
)


def _called_name(node: ast.Call) -> str | None:
    if isinstance(node.func, ast.Name):
        return node.func.id
    if isinstance(node.func, ast.Attribute):
        return node.func.attr
    return None


def test_ambiguous_verifier_is_retired_in_application_and_tests() -> None:
    for directory in ("app", "tests"):
        for path in (ROOT / directory).rglob("*.py"):
            tree = ast.parse(path.read_text(), filename=str(path))
            for node in ast.walk(tree):
                if isinstance(node, ast.Call):
                    assert _called_name(node) != RETIRED_VERIFIER, path
                if isinstance(node, ast.ImportFrom):
                    assert all(
                        alias.name != RETIRED_VERIFIER for alias in node.names
                    ), path
                if isinstance(node, ast.Constant) and isinstance(node.value, str):
                    # Includes monkeypatch/patch targets as well as direct calls.
                    assert node.value.rsplit(".", 1)[-1] != RETIRED_VERIFIER, path


def test_real_user_authentication_modules_use_user_verifier() -> None:
    for module in USER_MODULES:
        path = ROOT / "app" / "services" / f"{module}.py"
        tree = ast.parse(path.read_text())
        calls = [node for node in ast.walk(tree) if isinstance(node, ast.Call)]
        expected_calls = 2 if module in {"auth_flow", "web_system_db_inspector"} else 1
        assert (
            sum(_called_name(node) == "verify_user_password" for node in calls)
            == expected_calls
        ), path
        if module not in {"auth_flow", "web_customer_auth"}:
            assert all(
                _called_name(node) != "verify_service_secret" for node in calls
            ), path


def test_portal_verifiers_are_bound_to_their_credential_sources() -> None:
    path = ROOT / "app" / "services" / "web_customer_auth.py"
    tree = ast.parse(path.read_text())
    calls = [node for node in ast.walk(tree) if isinstance(node, ast.Call)]
    users = [node for node in calls if _called_name(node) == "verify_user_password"]
    services = [node for node in calls if _called_name(node) == "verify_service_secret"]
    assert len(users) == len(services) == 1
    assert ast.unparse(users[0].args[1]) == "local_credential.password_hash"
    assert "access_binding" in ast.unparse(services[0].args[1])
    guards = [
        node.test
        for node in ast.walk(tree)
        if isinstance(node, ast.If) and services[0] in ast.walk(node.test)
    ]
    assert any(
        isinstance(node, ast.Name) and node.id == "radius_authenticated"
        for guard in guards
        for node in ast.walk(guard)
    )


def test_access_login_uses_service_verifier_only() -> None:
    path = ROOT / "app" / "services" / "auth_flow.py"
    tree = ast.parse(path.read_text())
    function = next(
        node
        for node in tree.body
        if isinstance(node, ast.FunctionDef)
        and node.name == "_resolve_access_credential_match"
    )
    calls = {
        _called_name(node) for node in ast.walk(function) if isinstance(node, ast.Call)
    }
    assert "verify_service_secret" in calls
    assert "verify_user_password" not in calls
