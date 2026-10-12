"""Subscriber setup/confirmation have one authority and atomic recovery issuance."""

from __future__ import annotations

import ast
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def test_subscriber_legacy_writers_are_removed() -> None:
    tree = ast.parse((ROOT / "app/services/auth_flow.py").read_text())
    names = {node.name for node in ast.walk(tree) if isinstance(node, ast.FunctionDef)}
    assert not {"mfa_setup", "mfa_confirm"} & names
    assert {"reseller_mfa_setup", "reseller_mfa_confirm"} <= names


def test_adapters_do_not_reintroduce_subscriber_writers() -> None:
    for name in (
        "app/api/auth_flow.py",
        "app/api/reseller.py",
        "app/web/customer/routes.py",
        "app/services/web_reseller_routes.py",
    ):
        source = (ROOT / name).read_text()
        assert "auth_flow.mfa_setup(" not in source
        assert "auth_flow.mfa_confirm(" not in source
    customer = (ROOT / "app/web/customer/routes.py").read_text()
    start, end = (
        customer.index("def customer_mfa_confirm("),
        customer.index('@router.get("/contacts"'),
    )
    assert "generate_mfa_recovery_codes" not in customer[start:end]


def test_http_transport_adapter_stays_outside_service_layer() -> None:
    transport = ROOT / "app/subscriber_mfa_enrollment_http.py"
    service_adapter = ROOT / "app/services/subscriber_mfa_enrollment_adapter.py"
    assert transport.is_file()
    assert not service_adapter.exists()
    source = transport.read_text()
    assert "from fastapi import HTTPException" in source
    assert "owner.setup_subscriber_mfa" in source
    assert "owner.confirm_subscriber_mfa" in source


def test_concern_registered_and_lock_refresh_required() -> None:
    from app.services.sot_relationships import service_relationship

    service = service_relationship("auth.password_authentication")
    assert "authenticated subscriber MFA enrollment" in service.owns
    source = (ROOT / "app/services/password_authentication.py").read_text()
    start, end = (
        source.index("def _lock_subscriber_enrollment("),
        source.index("def _stage_subscriber_enrollment("),
    )
    lock = source[start:end]
    assert (
        lock.index("select(Subscriber)")
        < lock.index("select(AuthSession)")
        < lock.index("select(MFAMethod)")
    )
    assert lock.count("populate_existing=True") == 3
