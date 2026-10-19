"""Static boundary ratchet for the rollback-only historical Party participant."""

from __future__ import annotations

import ast
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def test_import_composition_never_owns_connections_or_transaction_completion() -> None:
    for name in (
        "app/services/party_kernel_import.py",
        "scripts/migration/kernel_party_import.py",
        "alembic_kernel_adoption/versions/ska0001_party_context.py",
    ):
        tree = ast.parse((ROOT / name).read_text())
        calls = [node for node in ast.walk(tree) if isinstance(node, ast.Call)]
        forbidden = {
            "commit",
            "rollback",
            "begin",
            "begin_nested",
            "begin_transaction",
            "create_engine",
            "stamp",
            "create_all",
        }
        for call in calls:
            function = call.func
            assert not (
                isinstance(function, ast.Attribute) and function.attr in forbidden
            ), name
            assert not (isinstance(function, ast.Name) and function.id in forbidden), (
                name
            )
        assert not any(
            isinstance(n, ast.If)
            and isinstance(n.test, ast.Compare)
            and isinstance(n.test.left, ast.Name)
            and n.test.left.id == "__name__"
            for n in ast.walk(tree)
        ), name


def test_historical_tenant_preserves_fields_without_provisioning() -> None:
    tree = ast.parse(
        (ROOT / "app/services/operator_tenant_kernel_import.py").read_text()
    )
    participant = next(
        n
        for n in tree.body
        if isinstance(n, ast.FunctionDef)
        and n.name == "stage_historical_operator_tenant"
    )
    calls = [n for n in ast.walk(participant) if isinstance(n, ast.Call)]
    assert not any(
        isinstance(n.func, ast.Attribute)
        and n.func.attr in {"commit", "rollback", "begin_nested"}
        for n in calls
    )
    assert not any(
        isinstance(n.func, ast.Name) and n.func.id == "provision_operator_tenant"
        for n in calls
    )
    source = ast.get_source_segment(
        (ROOT / "app/services/operator_tenant_kernel_import.py").read_text(),
        participant,
    )
    assert source is not None
    assert "suspended_at,deleted_at,created_at,updated_at" in source


def test_separate_history_and_database_side_json_are_explicit() -> None:
    source = (ROOT / "app/services/party_kernel_import.py").read_text()
    assert "merge_reason,metadata " in source
    assert (
        "json.loads" not in source
        and "json.dumps" not in source
        and "jsonb" not in source.lower()
    )
    runner = (ROOT / "scripts/migration/kernel_party_import.py").read_text()
    assert 'ADOPTION_VERSION_TABLE = "sub_kernel_adoption_alembic_version"' in runner
    ordinary = (ROOT / "alembic.ini").read_text()
    assert "alembic_kernel_adoption" not in ordinary
    migration = (
        ROOT / "alembic_kernel_adoption/versions/ska0001_party_context.py"
    ).read_text()
    assert migration.index("require_canonical_entry(connection") < migration.index(
        "CREATE TABLE"
    )
    assert "Populated Party context downgrade refused" in migration
    assert "GRANT " not in migration


def test_pipeline_verifies_destination_after_context_before_return() -> None:
    source = (ROOT / "scripts/migration/kernel_party_import.py").read_text()
    tree = ast.parse(source)
    pipeline = next(
        n
        for n in tree.body
        if isinstance(n, ast.FunctionDef) and n.name == "rehearse_party_import"
    )
    calls = {
        name: [
            n.lineno
            for n in ast.walk(pipeline)
            if isinstance(n, ast.Call)
            and isinstance(n.func, ast.Name)
            and n.func.id == name
        ]
        for name in (
            "stage_party_context",
            "verify_historical_operator_tenant",
            "verify_party_destinations",
        )
    }
    returned = next(n.lineno for n in pipeline.body if isinstance(n, ast.Return))
    assert (
        calls["stage_party_context"][0]
        < max(calls["verify_historical_operator_tenant"])
        < returned
    )
    assert (
        calls["stage_party_context"][0]
        < max(calls["verify_party_destinations"])
        < returned
    )
