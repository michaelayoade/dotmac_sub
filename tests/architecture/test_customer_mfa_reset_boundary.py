"""Prevent the retired customer MFA reset writers from returning."""

import ast
from pathlib import Path

from app.services import credential_administration as owner
from app.services import web_customer_user_access, web_system_user_mutations
from app.services.sot_relationships import service_relationship

ROOT = Path(__file__).resolve().parents[2]


def test_reset_has_exact_registered_concern_and_separate_scope():
    service = service_relationship(owner.OWNER)
    assert owner.CUSTOMER_MFA_RESET_CONCERN in service.owns
    assert owner.CUSTOMER_MFA_RESET_SCOPE == "customer:write"
    assert owner.CUSTOMER_MFA_RESET_SCOPE != owner.SCOPE
    assert service.contract is not None
    assert any(
        c.name == owner.CUSTOMER_MFA_RESET_CONCERN for c in service.contract.concerns
    )


def test_old_subscriber_writer_and_second_wrapper_audit_are_retired():
    assert not hasattr(web_system_user_mutations, "disable_subscriber_mfa")
    assert not hasattr(web_customer_user_access, "MFA_RESET_AUDIT_ACTION")
    tree = ast.parse((ROOT / "app/services/web_customer_user_access.py").read_text())
    function = next(
        node
        for node in tree.body
        if isinstance(node, ast.FunctionDef) and node.name == "reset_customer_mfa"
    )
    calls = [node for node in ast.walk(function) if isinstance(node, ast.Call)]
    assert len(calls) == 1
    assert (
        isinstance(calls[0].func, ast.Attribute)
        and calls[0].func.attr == "reset_customer_mfa"
    )
    assert {keyword.arg for keyword in calls[0].keywords} == {"command"}


def test_reset_adapter_uses_trusted_auth_fresh_session_and_safe_errors():
    source = (ROOT / "app/web/admin/customers.py").read_text()
    tree = ast.parse(source)
    function = next(
        node
        for node in tree.body
        if isinstance(node, ast.FunctionDef) and node.name == "customer_user_reset_mfa"
    )
    segment = ast.get_source_segment(source, function)
    assert segment is not None
    assert "request.state" in segment and 'auth.get("principal_id")' in segment
    assert "owner_command_session" in segment
    assert "get_current_user" not in segment
    assert 'actor.get("subscriber_id")' not in segment
    assert "log_customer_user_access_error" not in segment
    assert "str(exc)" not in segment and "require_admin" not in segment
    decorators = " ".join(
        ast.get_source_segment(source, node) or "" for node in function.decorator_list
    )
    assert 'require_permission("customer:write")' in decorators
