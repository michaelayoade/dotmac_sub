"""Guard the aggregate-only query and its declared read-only owner."""

import ast
from datetime import UTC, datetime
from pathlib import Path

from sqlalchemy.dialects import postgresql

from app.services.credential_security_inventory import (
    CredentialInventoryQuery,
    credential_inventory_statement,
)
from app.services.sot_manifest import TransactionMode
from app.services.sot_registry.registry import all_services, registry_validation_errors

QUERY = CredentialInventoryQuery(observed_at=datetime(2026, 10, 11, tzinfo=UTC))


def test_inventory_is_contracted_read_only_owner() -> None:
    service = next(
        s for s in all_services() if s.name == "auth.credential_security_inventory"
    )
    assert service.contract is not None
    assert service.contract.transaction.mode == TransactionMode.READ_ONLY
    assert not registry_validation_errors()


def test_query_projects_labels_not_credentials() -> None:
    statement = credential_inventory_statement(query=QUERY)
    assert tuple(statement.selected_columns.keys()) == (
        "principal",
        "provider",
        "format",
        "password_age",
        "last_login_age",
        "active",
        "count",
    )
    sql = str(statement.compile(dialect=postgresql.dialect()))
    assert "GROUP BY" in sql
    assert "access_credentials" not in sql
    assert "radius_users" not in sql
    assert "username" not in sql


def test_no_mutation_or_secret_resolution_and_canonical_snapshot_adapter() -> None:
    root = Path(__file__).resolve().parents[2]
    owner = root / "app/services/credential_security_inventory.py"
    tree = ast.parse(owner.read_text())
    forbidden = {
        "commit",
        "flush",
        "add",
        "delete",
        "merge",
        "update",
        "decrypt_credential",
        "resolve_secret",
        "get_secret",
    }
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            name = (
                node.func.attr
                if isinstance(node.func, ast.Attribute)
                else (node.func.id if isinstance(node.func, ast.Name) else "")
            )
            assert name not in forbidden
    adapter = root / "scripts/authentication/credential_security_inventory.py"
    assert "with read_only_snapshot_session() as db:" in adapter.read_text()
