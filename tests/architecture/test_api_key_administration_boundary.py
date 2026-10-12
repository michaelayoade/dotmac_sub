"""Prevent the retired JSON API-key revocation writer from returning."""

import ast
from pathlib import Path

from app.api.auth import router
from app.services import auth
from app.services.api_key_administration import CONCERN, OWNER
from app.services.sot_registry.registry import service_relationship
from tests.architecture.test_credential_minting_route_guards import (
    _find_route,
    _role_guards,
)


def test_exact_owner_and_retired_fallback() -> None:
    service = service_relationship(OWNER)
    assert service.owns == (CONCERN,)
    assert service.contract is not None
    assert not hasattr(auth.ApiKeys, "delete")
    assert not hasattr(auth.ApiKeys, "revoke")


def test_delete_adapter_uses_fresh_session_and_admin_actor() -> None:
    route = _find_route(router, "DELETE", "/api-keys/{key_id}")
    assert "admin" in _role_guards(route.dependant)
    tree = ast.parse(Path("app/api/auth.py").read_text())
    endpoint = next(
        node
        for node in tree.body
        if isinstance(node, ast.FunctionDef) and node.name == "delete_api_key"
    )
    source = ast.unparse(endpoint)
    assert "owner_command_session" in source
    assert "api_key_admin.revoke(command_db, command=command)" in source
    assert "Depends(get_db)" not in source
    assert "auth_service.api_keys" not in source


def test_owner_has_one_executor_and_no_transaction_completion() -> None:
    tree = ast.parse(Path("app/services/api_key_administration.py").read_text())
    calls = [node.func for node in ast.walk(tree) if isinstance(node, ast.Call)]
    assert (
        sum(
            isinstance(call, ast.Name) and call.id == "execute_owner_command"
            for call in calls
        )
        == 1
    )
    assert not any(
        isinstance(call, ast.Attribute)
        and call.attr in {"commit", "rollback", "begin", "begin_nested"}
        for call in calls
    )
