"""Password-only credential administration cannot regain generic writers."""

import ast
from pathlib import Path

from app.schemas.auth import UserCredentialCreate, UserCredentialUpdate
from app.services import auth
from app.services.sot_manifest import TransactionMode
from app.services.sot_relationships import service_relationship

ROOT = Path(__file__).resolve().parents[2]


def test_credential_write_allowlists_and_retired_generic_writers():
    assert set(UserCredentialCreate.model_fields) == {
        "subscriber_id",
        "provider",
        "username",
        "radius_server_id",
        "password",
    }
    assert set(UserCredentialUpdate.model_fields) == {
        "username",
        "radius_server_id",
        "password",
    }
    assert not any(
        hasattr(auth.user_credentials, name) for name in ("create", "update", "delete")
    )


def test_credential_commands_and_participants_have_typed_contracts():
    for name, mode in (
        ("auth.credential_administration", TransactionMode.COORDINATOR_MANAGED),
        ("auth.credential_administration_mutation", TransactionMode.PARTICIPANT),
        ("customer.registration_metadata", TransactionMode.PARTICIPANT),
    ):
        service = service_relationship(name)
        assert service.contract is not None
        assert service.contract.transaction.mode == mode


def test_participants_never_complete_transactions_and_form_never_hashes():
    for name in (
        "credential_administration_mutation",
        "customer_registration_metadata",
    ):
        source = (ROOT / f"app/services/{name}.py").read_text()
        tree = ast.parse(source)
        assert not any(
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr in {"commit", "rollback", "begin_nested"}
            for node in ast.walk(tree)
        )
        assert "execute_owner_command" not in source
    form = (ROOT / "app/services/web_subscriber_forms.py").read_text()
    assert "hash_password" not in form
    assert "UserCredentialCreate" not in form
    assert "subscribers.update" not in form


def test_credential_write_routes_require_admin_and_permission():
    source = (ROOT / "app/api/auth.py").read_text()
    tree = ast.parse(source)
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name in {
            "create_user_credential",
            "update_user_credential",
            "delete_user_credential",
        }:
            declaration = ast.get_source_segment(source, node.decorator_list[0])
            assert "_credential_admin" in declaration
            assert 'require_permission("auth:credential:write")' in declaration
