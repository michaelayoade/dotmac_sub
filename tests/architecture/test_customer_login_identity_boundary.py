"""Keep customer email-login selection behind its typed resolver boundary."""

from __future__ import annotations

import ast
from pathlib import Path

from app.services.sot_registry.registry import service_relationship

PROJECT_ROOT = Path(__file__).resolve().parents[2]
OWNER = PROJECT_ROOT / "app" / "services" / "customer_login_identity.py"
RESOLVER_CONSUMERS = (
    PROJECT_ROOT / "app" / "services" / "auth_flow.py",
    PROJECT_ROOT / "app" / "services" / "customer_authentication.py",
)


def _calls(path: Path) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    return {
        node.func.attr
        for node in ast.walk(tree)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
    }


def test_customer_login_identity_has_complete_read_only_contract() -> None:
    service = service_relationship("auth.customer_login_identity")

    assert service.module == "app.services.customer_login_identity"
    assert service.contract is not None
    assert service.contract.migration.state.value == "complete"
    assert service.contract.transaction.mode.value == "read_only"


def test_customer_authentication_policies_use_the_typed_resolver() -> None:
    for consumer in RESOLVER_CONSUMERS:
        source = consumer.read_text(encoding="utf-8")
        assert "ResolveCustomerLoginIdentity(" in source
        assert "resolve_customer_login_identity" in _calls(consumer)


def test_resolver_is_read_only_and_transport_neutral() -> None:
    source = OWNER.read_text(encoding="utf-8")
    calls = _calls(OWNER)

    assert not {"add", "delete", "flush", "commit", "rollback"} & calls
    assert "HTTPException" not in source
    assert "RedirectResponse" not in source
