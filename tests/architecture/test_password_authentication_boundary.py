"""Pin the credential-standing owner's boundaries.

These are the properties that fail silently if they regress: every behaviour
test still passes while a second writer, an adapter commit, or a stray
representation-GUC reopens the login/reset race.
"""

from __future__ import annotations

import ast
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[2]
OWNER = PROJECT_ROOT / "app" / "services" / "password_authentication.py"
AUTH_FLOW = PROJECT_ROOT / "app" / "services" / "auth_flow.py"
MIGRATION = PROJECT_ROOT / "alembic" / "versions" / "669_user_credential_version.py"
GUC = "app.credential_change_kind"


def _tree(path: Path) -> ast.Module:
    return ast.parse(path.read_text(encoding="utf-8"), filename=str(path))


def _function(tree: ast.AST, name: str) -> ast.FunctionDef:
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name == name:
            return node
    raise AssertionError(f"function {name} not found")


def _attr_calls(node: ast.AST) -> set[str]:
    return {
        call.func.attr
        for call in ast.walk(node)
        if isinstance(call, ast.Call) and isinstance(call.func, ast.Attribute)
    }


def _name_calls(node: ast.AST) -> set[str]:
    return {
        call.func.id
        for call in ast.walk(node)
        if isinstance(call, ast.Call) and isinstance(call.func, ast.Name)
    }


def test_owner_is_registered_with_its_four_concerns() -> None:
    from app.services.sot_relationships import service_relationship

    service = service_relationship("auth.password_authentication")
    assert service.module == "app.services.password_authentication"
    assert set(service.owns) == {
        "password authentication success transition",
        "MFA challenge completion",
        "authentication failure counters",
        "authenticated password change",
    }


def test_owner_commits_only_through_the_owner_command_boundary() -> None:
    tree = _tree(OWNER)
    source = OWNER.read_text(encoding="utf-8")
    assert "execute_owner_command" in _name_calls(tree)
    assert ".commit(" not in source
    assert "HTTPException" not in source
    # The one permitted rollback is the phase A -> phase B hand-off, which
    # first asserts the session holds no pending writes.
    rollback_owners = {
        function.name
        for function in ast.walk(tree)
        if isinstance(function, ast.FunctionDef) and "rollback" in _attr_calls(function)
    }
    assert rollback_owners == {"release_read_transaction"}


def test_session_staging_never_commits() -> None:
    staged = _function(_tree(AUTH_FLOW), "stage_session_issue")
    assert not {"commit", "rollback"} & _attr_calls(staged)


def test_login_and_mfa_adapters_do_not_commit_or_issue_legacy_sessions() -> None:
    tree = _tree(AUTH_FLOW)
    for name in ("login", "mfa_verify", "establish_enrolled_session"):
        function = _function(tree, name)
        assert "commit" not in _attr_calls(function), name
        assert "_issue_tokens" not in _attr_calls(function), name
        assert "_issue_tokens_once" not in _attr_calls(function), name


def test_change_password_commits_nothing_itself() -> None:
    function = _function(_tree(AUTH_FLOW), "change_password")
    assert "commit" not in _attr_calls(function)
    assert "apply_password_change" in _attr_calls(function)


def test_pppoe_branch_never_names_the_local_credential_counters() -> None:
    """R8: the access-credential success path has no UserCredential write."""

    source = OWNER.read_text(encoding="utf-8")
    gate = ast.get_source_segment(
        source, _function(_tree(OWNER), "_gate_password_step")
    )
    assert gate is not None
    pppoe_branch = gate.split("values: dict", 1)[0]
    assert "UserCredential" not in pppoe_branch
    assert "AccessCredential" in pppoe_branch


def test_representation_guc_is_named_by_exactly_one_function() -> None:
    offenders: list[str] = []
    for path in sorted((PROJECT_ROOT / "app").rglob("*.py")):
        text = path.read_text(encoding="utf-8")
        if GUC not in text:
            continue
        if path != OWNER:
            offenders.append(str(path.relative_to(PROJECT_ROOT)))
            continue
        for function in ast.walk(_tree(path)):
            if not isinstance(function, ast.FunctionDef):
                continue
            segment = ast.get_source_segment(text, function) or ""
            if GUC in segment and function.name != "replace_password_representation":
                offenders.append(f"{path.name}:{function.name}")
    assert not offenders, (
        f"{GUC} may only be set by replace_password_representation: {offenders}"
    )
    assert GUC in OWNER.read_text(encoding="utf-8")


def test_representation_hook_is_not_called_by_login() -> None:
    """Hash upgrades are a later, separately-gated change."""

    offenders = [
        str(path.relative_to(PROJECT_ROOT))
        for path in sorted((PROJECT_ROOT / "app").rglob("*.py"))
        if path != OWNER
        and (
            "replace_password_representation" in _name_calls(_tree(path))
            or "replace_password_representation" in _attr_calls(_tree(path))
        )
    ]
    assert not offenders, offenders
    owner_tree = _tree(OWNER)
    callers = [
        function.name
        for function in ast.walk(owner_tree)
        if isinstance(function, ast.FunctionDef)
        and function.name != "replace_password_representation"
        and "replace_password_representation" in _name_calls(function)
    ]
    assert not callers, callers


def test_credential_version_never_leaves_the_service_boundary() -> None:
    schema = (PROJECT_ROOT / "app" / "schemas" / "auth.py").read_text(encoding="utf-8")
    assert "credential_version" not in schema


def test_migration_669_sits_on_668_and_installs_the_trigger() -> None:
    source = MIGRATION.read_text(encoding="utf-8")
    assert (
        'down_revision: str | None = "668_sole_approver_adjudication_evidence"'
        in source
    )
    assert "BEFORE UPDATE ON user_credentials" in source
    assert GUC in source
    assert "SET LOCAL lock_timeout" in source
    assert "NOT VALID" in source and "VALIDATE CONSTRAINT" in source
    assert "DROP TRIGGER" in source and "DROP FUNCTION" in source


def test_reset_bumps_credential_version_under_its_row_lock() -> None:
    source = (PROJECT_ROOT / "app" / "services" / "credential_recovery.py").read_text(
        encoding="utf-8"
    )
    assert "credential.credential_version" in source
