"""``customer.account_recovery`` is fail-closed, typed, and participant-gated."""

from __future__ import annotations

import ast
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SERVICE = ROOT / "app/services/account_recovery.py"
RESTORE_TOOL = ROOT / "app/services/web_system_restore_tool.py"


def _source(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def test_only_subscription_is_a_registered_participant() -> None:
    source = _source(SERVICE)
    assert 'REGISTERED_RECOVERY_PARTICIPANTS: frozenset[str] = frozenset({"subscription"})' in (
        source
    )


def test_service_never_raises_or_returns_http_exception() -> None:
    """No `import fastapi` / `from fastapi import ...` and no `raise
    HTTPException(...)` anywhere — prose mentioning the word in a docstring
    explaining this exact rule is fine and expected."""
    source = _source(SERVICE)
    tree = ast.parse(source, filename=str(SERVICE))
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module == "fastapi":
            raise AssertionError("account_recovery.py must not import fastapi")
        if isinstance(node, ast.Import) and any(
            alias.name == "fastapi" for alias in node.names
        ):
            raise AssertionError("account_recovery.py must not import fastapi")
        if isinstance(node, ast.Raise) and node.exc is not None:
            call = node.exc
            name = None
            if isinstance(call, ast.Call) and isinstance(call.func, ast.Name):
                name = call.func.id
            elif isinstance(call, ast.Name):
                name = call.id
            assert name != "HTTPException"


def test_restore_tool_no_longer_imports_participant_models() -> None:
    """The retired invoice/payment/service-order/credential/RADIUS/IP/ONT
    /splitter/CPE participant imports must be gone.

    Checked over actual `import`/`from ... import` statements (AST), not a
    substring scan, so prose in the module docstring explaining this exact
    removal does not trip the check.
    """
    source = _source(RESTORE_TOOL)
    tree = ast.parse(source, filename=str(RESTORE_TOOL))
    forbidden_modules = {
        "app.models.billing",
        "app.models.network",
        "app.models.provisioning",
        "app.models.radius",
        "fastapi",
    }
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module in forbidden_modules:
            raise AssertionError(f"forbidden import: {node.module}")
        if isinstance(node, ast.Import):
            for alias in node.names:
                assert alias.name not in forbidden_modules, alias.name
    assert "access_credential_service" not in source


def test_restore_tool_delegates_mutation_to_account_recovery() -> None:
    source = _source(RESTORE_TOOL)
    assert "from app.services import account_recovery" in source
    assert "account_recovery.restore_account(" in source
    assert "account_recovery.rebaseline_recovery_evidence(" in source
    # No direct ORM mutation of any of the retired participant tables.
    for forbidden in (
        "invoice.is_active",
        "payment.is_active",
        "credential.is_active",
        "radius_user.is_active",
        "ip_assignment.is_active",
        "ont_assignment.active",
        "splitter_assignment.active",
    ):
        assert forbidden not in source


def test_build_page_state_is_read_only() -> None:
    """No mutation, flush, commit, or purge call in `build_page_state`'s
    BODY. Checked by AST over the function's statements, not a substring
    scan of the whole function text, so its own docstring (which explains,
    in prose, that the old purge call was removed) cannot trip the check."""
    source = _source(RESTORE_TOOL)
    tree = ast.parse(source, filename=str(RESTORE_TOOL))
    target = next(
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.FunctionDef) and node.name == "build_page_state"
    )
    forbidden_calls = {"commit", "flush", "add", "delete", "execute"}
    forbidden_names = {"purge_expired_from_recovery_queue"}
    for node in ast.walk(target):
        if node is target:
            continue
        if isinstance(node, ast.Call):
            func = node.func
            if isinstance(func, ast.Attribute) and func.attr in forbidden_calls:
                raise AssertionError(f"forbidden call: .{func.attr}(")
            if isinstance(func, ast.Name) and func.id in forbidden_names:
                raise AssertionError(f"forbidden call: {func.id}(")
    # `purged_count` must not be a returned dict key. Check the literal
    # string keys of any dict this function constructs and returns, not the
    # whole dumped tree (which would also match the docstring's prose).
    for node in ast.walk(target):
        if isinstance(node, ast.Dict):
            for key in node.keys:
                if isinstance(key, ast.Constant) and key.value == "purged_count":
                    raise AssertionError("build_page_state must not return purged_count")
