"""Prevent retired JSON API-key issuance/revocation writers from returning."""

import ast
from pathlib import Path

from app.api.auth import router
from app.services import auth
from app.services.api_key_administration import (
    CONCERN,
    ISSUANCE_CONCERN,
    OWNER,
    UPDATE_CONCERN,
)
from app.services.sot_registry.registry import service_relationship
from tests.architecture.test_credential_minting_route_guards import (
    _find_route,
    _role_guards,
)


def test_exact_owner_and_retired_fallback() -> None:
    service = service_relationship(OWNER)
    assert service.owns == (CONCERN, ISSUANCE_CONCERN, UPDATE_CONCERN)
    assert service.contract is not None
    assert not hasattr(auth.ApiKeys, "delete")
    assert not hasattr(auth.ApiKeys, "revoke")
    for method in ("create", "generate", "generate_with_rate_limit", "update"):
        assert not hasattr(auth.ApiKeys, method)


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
    for name in ("create", "generate", "revoke", "update"):
        public = next(
            node
            for node in tree.body
            if isinstance(node, ast.FunctionDef) and node.name == name
        )
        assert (
            sum(
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Name)
                and node.func.id == "execute_owner_command"
                for node in ast.walk(public)
            )
            == 1
        )
    participant = next(
        node
        for node in tree.body
        if isinstance(node, ast.FunctionDef) and node.name == "_stage_issuance"
    )
    assert "execute_owner_command" not in ast.unparse(participant)
    assert "stage_audit_event" in ast.unparse(participant)
    assert not any(
        isinstance(call, ast.Attribute)
        and call.attr in {"commit", "rollback", "begin", "begin_nested"}
        for call in calls
    )


def test_issuance_adapters_use_fresh_sessions_and_admin_actor() -> None:
    tree = ast.parse(Path("app/api/auth.py").read_text())
    for name, path, method in (
        ("create_api_key", "/api-keys", "create"),
        ("generate_api_key", "/api-keys/generate", "generate"),
    ):
        route = _find_route(router, "POST", path)
        assert "admin" in _role_guards(route.dependant)
        endpoint = next(
            node
            for node in tree.body
            if isinstance(node, ast.FunctionDef) and node.name == name
        )
        source = ast.unparse(endpoint)
        assert "owner_command_session" in source
        assert f"api_key_admin.{method}(command_db, command=command)" in source
        assert "api_keys.create(" not in source
        assert "api_keys.generate_with_rate_limit(" not in source


def test_patch_adapter_uses_owner_and_fresh_session() -> None:
    route = _find_route(router, "PATCH", "/api-keys/{key_id}")
    assert "admin" in _role_guards(route.dependant)
    tree = ast.parse(Path("app/api/auth.py").read_text())
    endpoint = next(
        node
        for node in tree.body
        if isinstance(node, ast.FunctionDef) and node.name == "update_api_key"
    )
    source = ast.unparse(endpoint)
    assert "owner_command_session" in source
    assert "api_key_admin.update(command_db, command=command)" in source
    assert "Depends(get_db)" not in source
    assert "auth_service.api_keys" not in source


def test_update_owner_uses_closed_field_allowlist_and_refreshed_lock() -> None:
    source = Path("app/services/api_key_administration.py").read_text()
    tree = ast.parse(source)
    endpoint = next(
        node
        for node in tree.body
        if isinstance(node, ast.FunctionDef) and node.name == "update"
    )
    update = ast.unparse(endpoint)
    assert ".with_for_update()" in update
    assert "populate_existing=True" in update
    assert "set_config" in update
    assert "setattr(" not in update
    assert "key.system_user_id =" not in update
    fields = next(
        node
        for node in tree.body
        if isinstance(node, ast.ClassDef) and node.name == "ApiKeyUpdateField"
    )
    assert {
        node.value.value for node in fields.body if isinstance(node, ast.Assign)
    } == {
        "subscriber_id",
        "label",
        "key_hash",
        "scopes",
        "is_active",
        "last_used_at",
        "expires_at",
        "revoked_at",
    }
