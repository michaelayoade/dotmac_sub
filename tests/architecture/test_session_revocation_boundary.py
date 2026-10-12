"""Retirement guards for the bounded atomic session revocation surface."""

import ast
from pathlib import Path

from app.services.session_manager import (
    RevocationIntent,
    RevocationProvenance,
    RevokeSessionsCommand,
)
from app.services.sot_manifest import OwnerRole, TransactionMode
from app.services.sot_relationships import service_relationship

ROOT = Path(__file__).resolve().parents[2]


def _calls(path, function):
    tree = ast.parse((ROOT / path).read_text())
    node = next(
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.FunctionDef) and node.name == function
    )
    return [
        node.func.attr
        if isinstance(node.func, ast.Attribute)
        else node.func.id
        if isinstance(node.func, ast.Name)
        else ""
        for node in ast.walk(node)
        if isinstance(node, ast.Call)
    ]


def test_revocation_has_one_contracted_executor_and_canonical_audit():
    calls = _calls("app/services/session_manager.py", "revoke_authentication_sessions")
    assert calls.count("execute_owner_command") == 1
    assert calls.count("stage_audit_event") == 1
    assert not {
        "commit",
        "rollback",
        "record",
        "invalidate_session_context",
        "invalidate_principal",
    }.intersection(calls)
    owner = service_relationship("app_sessions.auth")
    assert owner.contract.transaction.mode is TransactionMode.OWNER_MANAGED
    concern = next(
        item
        for item in owner.contract.concerns
        if item.name == "database auth-session revocation"
    )
    assert (
        concern.role is OwnerRole.COMMAND_WRITER
        and concern.canonical_writer == owner.name
    )
    projection = next(
        item
        for item in owner.contract.projections
        if item.name == "exact revoked authentication session cache"
    )
    assert projection.writer == projection.repair_owner == owner.name
    assert "exact" in projection.rebuild_operation


def test_covered_transports_cannot_restore_direct_mutation_or_cache_cleanup():
    surfaces = [
        ("app/services/auth_flow.py", "logout"),
        ("app/services/auth.py", "delete"),
        ("app/api/auth.py", "delete_session"),
        ("app/api/auth_flow.py", "revoke_session"),
        ("app/api/auth_flow.py", "revoke_all_other_sessions"),
        ("app/web/admin/system.py", "user_profile_sign_out_other_sessions"),
    ]
    # Sessions.delete is selected by class; auth.py contains other legacy deletes.
    for path, function in surfaces:
        if path == "app/services/auth.py":
            tree = ast.parse((ROOT / path).read_text())
            cls = next(
                node
                for node in ast.walk(tree)
                if isinstance(node, ast.ClassDef) and node.name == "Sessions"
            )
            node = next(
                node
                for node in cls.body
                if isinstance(node, ast.FunctionDef) and node.name == function
            )
            calls = [
                node.func.attr
                for node in ast.walk(node)
                if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
            ]
        else:
            calls = _calls(path, function)
        assert not {
            "commit",
            "rollback",
            "invalidate_session_context",
            "invalidate_principal",
        }.intersection(calls), path


def test_projection_is_strict_exact_target_and_bearer_is_ephemeral():
    calls = _calls(
        "app/services/events/handlers/session_revocation_projection.py", "handle"
    )
    assert "invalidate_session_context_strict" in calls
    assert not {
        "invalidate_principal",
        "invalidate_principal_strict",
        "revoke_principal",
        "commit",
    }.intersection(calls)
    assert RevokeSessionsCommand.__dataclass_fields__["refresh_token"].repr is False
    assert len(RevocationIntent) == 5 and len(RevocationProvenance) == 4
