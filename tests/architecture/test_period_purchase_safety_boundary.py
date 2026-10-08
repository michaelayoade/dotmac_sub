"""Prevent parallel period-purchase cash allocation and transaction writers."""

import ast
from pathlib import Path

from app.services.sot_registry.registry import all_services, registry_validation_errors

ROOT = Path(__file__).resolve().parents[2]


def _tree(relative: str) -> ast.Module:
    return ast.parse((ROOT / relative).read_text(encoding="utf-8"))


def test_period_purchase_and_outage_participants_never_complete_transactions():
    for relative in (
        "app/services/prepaid_period_purchases.py",
        "app/services/outage_compensation.py",
    ):
        prohibited = [
            node.attr
            for node in ast.walk(_tree(relative))
            if isinstance(node, ast.Attribute)
            and node.attr in {"commit", "rollback", "begin_nested"}
        ]
        assert not prohibited, (relative, prohibited)


def test_gateway_reconciliation_purchase_branch_ends_before_generic_allocation():
    tree = _tree("app/services/payment_reconciliation.py")
    owner = next(
        node
        for node in tree.body
        if isinstance(node, ast.FunctionDef)
        and node.name == "_stage_verified_settlement_for_intent"
    )
    branch = next(
        node
        for node in owner.body
        if isinstance(node, ast.If)
        and "prepaid_period_purchase" in ast.unparse(node.test)
    )
    assert isinstance(branch.body[-1], ast.Return)
    calls = {
        ast.unparse(node.func)
        for node in ast.walk(branch)
        if isinstance(node, ast.Call)
    }
    assert "stage_verified_prepaid_period_purchase" in calls
    assert not any(
        "auto_allocate" in call or "allocate_payment" in call for call in calls
    )


def test_repair_and_coverage_boundaries_are_registered_to_their_owners():
    assert registry_validation_errors() == ()
    expected = {
        "financial.compensated_service_time": {
            "compensated service clock claims",
            "compensated service clock history",
        },
        "financial.prepaid_period_purchases": {
            "reviewed prepaid purchase receipt recovery",
        },
        "financial.outage_compensation": {
            "reviewed outage compensation recovery",
            "outage compensation funding retraction",
        },
        "financial.purchased_service_coverage": {
            "purchased service coverage protection"
        },
        "financial.purchase_payment_recovery_state": {
            "confirmed purchase payment recovery state"
        },
    }
    for name, concerns in expected.items():
        service = next(service for service in all_services() if service.name == name)
        assert concerns <= set(service.owns)
        assert service.contract is not None
        assert concerns <= {concern.name for concern in service.contract.concerns}


def test_purchase_migration_preserves_original_revision_identities_and_merges_main():
    graph = {}
    for path in (ROOT / "alembic/versions").glob("*.py"):
        values = {}
        for node in ast.parse(path.read_text(encoding="utf-8")).body:
            if isinstance(node, ast.Assign):
                name = (
                    node.targets[0].id if isinstance(node.targets[0], ast.Name) else ""
                )
            elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
                name = node.target.id
            else:
                continue
            if name in {"revision", "down_revision"}:
                values[name] = ast.literal_eval(node.value)
        if values.get("revision"):
            parents = values.get("down_revision")
            graph[values["revision"]] = (
                tuple(parents)
                if isinstance(parents, (list, tuple))
                else ((parents,) if parents else ())
            )
    assert set(graph["655_prepaid_purchase_current_main_merge"]) == {
        "647_purchase_outage_approval",
        "650_customer_connection_type",
    }
    assert set(graph["657_purchase_prepaid_sweep_merge"]) == {
        "655_prepaid_purchase_current_main_merge",
        "651_prepaid_sweep_cycle_totals",
    }
    head = "646_prepaid_purchase_safety"
    assert set(graph[head]) == {
        "642_network_map_import_feature_classification",
        "637_prepaid_period_purchase_intent_contract",
    }
    assert graph["637_prepaid_period_purchase_intent_contract"] == (
        "636_service_period_purchase_contract",
    )
    ancestors = set()
    remaining = [head]
    while remaining:
        revision = remaining.pop()
        if revision in ancestors:
            continue
        ancestors.add(revision)
        remaining.extend(graph[revision])
    assert {
        "636_service_period_purchase_contract",
        "642_network_map_import_feature_classification",
    } <= ancestors
    # This is a source graph check. Real migration execution remains a PG gate.
