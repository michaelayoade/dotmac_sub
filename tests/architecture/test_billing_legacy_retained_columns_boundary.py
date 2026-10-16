"""Closed retained money policy stays private under the existing fence owner."""

import ast
from pathlib import Path

from app.services.billing.legacy_retained_columns_contracts import RetainedRelation
from app.services.sot_registry.registry import service_relationship
from tests.architecture.test_billing_legacy_archive_fence_boundary import (
    _assert_private_preparation_calls,
)

ROOT = Path(__file__).resolve().parents[2]
MODULE = ROOT / "app/services/billing/legacy_retained_columns_fence.py"
PREPARATION = ROOT / "app/services/billing/legacy_handoff_preparation.py"


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
            if path == PREPARATION:
                _assert_private_preparation_calls(
                    tree, "_install_legacy_retained_columns_fence"
                )
                continue
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


def test_retained_attachment_union_and_shared_closure_order() -> None:
    from app.services.billing.legacy_privilege_closure import (
        ClosurePopulation,
        _population,
    )

    assert _population(ClosurePopulation.RETAINED) == (
        "billing_accounts",
        "outbox_events",
        "platform_outbox_events",
        "subscribers",
    )
    assert len(_population(ClosurePopulation.RETAINED)) == 4
    tree = ast.parse(MODULE.read_text())
    installer = next(
        node
        for node in tree.body
        if isinstance(node, ast.FunctionDef)
        and node.name == "_install_legacy_retained_columns_fence"
    )
    positions = {
        name: min(
            node.lineno
            for node in ast.walk(installer)
            if isinstance(node, ast.Call)
            and isinstance(node.func, ast.Name)
            and node.func.id == name
        )
        for name in (
            "_lock_privilege_population",
            "_observe_privilege_closure",
            "_close_attachment_privileges",
        )
    }
    assert (
        positions["_lock_privilege_population"]
        < positions["_observe_privilege_closure"]
        < positions["_close_attachment_privileges"]
    )
