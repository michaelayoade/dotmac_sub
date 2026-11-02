"""Keep Inbox identity review in registered owners and admin correction gated."""

from __future__ import annotations

import ast
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def _function(module: ast.Module, name: str) -> ast.FunctionDef:
    return next(
        node
        for node in module.body
        if isinstance(node, ast.FunctionDef) and node.name == name
    )


def test_identity_review_routes_delegate_writes_to_typed_owners() -> None:
    source = (ROOT / "app/web/admin/inbox.py").read_text(encoding="utf-8")
    tree = ast.parse(source)
    for name in (
        "team_inbox_reviewed_lead_selection",
        "team_inbox_request_identity_correction",
        "team_inbox_remove_incorrect_contact",
        "team_inbox_clear_identity_correction",
    ):
        method = _function(tree, name)
        calls = {
            ast.unparse(node.func)
            for node in ast.walk(method)
            if isinstance(node, ast.Call)
        }
        assert any(call.endswith("Command") for call in calls), name
        assert not any(
            call.endswith(".commit") or call.endswith(".rollback") for call in calls
        ), name
    for name in (
        "team_inbox_remove_incorrect_contact",
        "team_inbox_clear_identity_correction",
    ):
        assert "_is_identity_admin(request, db)" in ast.unparse(_function(tree, name))


def test_identity_correction_owner_uses_the_registered_command_boundary() -> None:
    source = (ROOT / "app/services/team_inbox_contact_links.py").read_text(
        encoding="utf-8"
    )
    tree = ast.parse(source)
    for name in (
        "request_identity_correction",
        "clear_identity_correction",
        "remove_incorrect_party_contact",
    ):
        method = _function(tree, name)
        calls = {
            ast.unparse(node.func)
            for node in ast.walk(method)
            if isinstance(node, ast.Call)
        }
        assert "execute_owner_command" in calls
        assert "stage_audit_event" in calls
        assert "emit_event" in calls
        assert "db.commit" not in calls
