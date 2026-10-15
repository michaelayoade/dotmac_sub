"""Closed retained money policy stays private under the existing fence owner."""

import ast
from pathlib import Path

from app.services.billing.legacy_retained_columns_contracts import RetainedRelation
from app.services.sot_registry.registry import service_relationship

ROOT = Path(__file__).resolve().parents[2]
MODULE = ROOT / "app/services/billing/legacy_retained_columns_fence.py"


def test_closed_policy_and_existing_owner() -> None:
    assert {r.value for r in RetainedRelation} == {"billing_accounts", "subscribers"}
    owner = service_relationship("runtime.billing_legacy_archive_fence")
    assert (
        "app.services.billing.legacy_retained_columns_fence"
        in owner.implementation_modules
    )
    assert owner.contract is not None and owner.contract.events is not None
    assert "billing.legacy_retained_columns.staged" in owner.contract.events.event_types


def test_private_flush_only_and_final_tuple_guards() -> None:
    tree = ast.parse(MODULE.read_text())
    assert not [
        n
        for n in tree.body
        if isinstance(n, ast.FunctionDef) and not n.name.startswith("_")
    ]
    calls = {
        n.func.attr if isinstance(n.func, ast.Attribute) else n.func.id
        for n in ast.walk(tree)
        if isinstance(n, ast.Call) and isinstance(n.func, (ast.Name, ast.Attribute))
    }
    assert (
        not {
            "commit",
            "rollback",
            "begin",
            "begin_nested",
            "execute_owner_command",
            "create_engine",
            "Session",
        }
        & calls
    )
    source = MODULE.read_text()
    assert (
        "from app.services.billing.legacy_archive_fence import _posture, _quote, _root"
        in source
    )
    assert '"AFTER", "INSERT OR UPDATE"' in source
    assert "NEW.id IS NOT DISTINCT FROM OLD.id" in source
    assert "NEW.deposit IS NULL" in source and "OLD.deposit IS NULL" in source
    assert "REVOKE TRUNCATE ON TABLE" in source
    assert "SECURITY INVOKER SET search_path TO pg_catalog" in source


def test_no_production_caller() -> None:
    for directory in ("app", "scripts", "alembic"):
        for path in (ROOT / directory).rglob("*.py"):
            if path == MODULE:
                continue
            tree = ast.parse(path.read_text())
            assert not any(
                isinstance(n, ast.Call)
                and (
                    (
                        isinstance(n.func, ast.Name)
                        and n.func.id == "_install_legacy_retained_columns_fence"
                    )
                    or (
                        isinstance(n.func, ast.Attribute)
                        and n.func.attr == "_install_legacy_retained_columns_fence"
                    )
                )
                for n in ast.walk(tree)
            ), path
