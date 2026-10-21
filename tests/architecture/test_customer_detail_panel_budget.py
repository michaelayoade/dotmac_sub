import ast
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def _source(path: str) -> str:
    return (ROOT / path).read_text(encoding="utf-8")


def test_initial_customer_detail_defers_network_projection() -> None:
    route = _source("app/web/admin/customers.py")

    assert "CustomerDetailNetworkQuery(" in route
    assert 'include=panel == "network"' in route


def test_network_panel_is_lazy_and_selects_only_its_fragment() -> None:
    template = _source("templates/admin/customers/detail.html")

    assert 'id="customer-network-panel"' in template
    assert 'id="customer-network-panel-content"' in template
    assert 'hx-trigger="revealed"' in template
    assert 'hx-select="#customer-network-panel-content"' in template
    assert 'hx-select="#customer-network-panel"' not in template
    assert template.index("x-show=\"activeTab === 'network'\"") < template.index(
        'id="customer-network-panel-content"'
    )
    assert (
        "if (hash === 'network') {\n                this.activeTab = 'network';"
        in template
    )
    assert "network_access_is_bounded" in template
    assert "{% if sub_conn %}" in template


def test_financial_balance_projection_uses_cohort_owner() -> None:
    service = _source("app/services/web_customer_details.py")

    assert "profiles = resolve_billing_profiles(db, accounts)" in service
    assert "prepaid_available_balances(db, prepaid_account_ids)" in service
    assert "get_available_balance" not in service
    tree = ast.parse(service)
    projection_gates = [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.If)
        and any(
            isinstance(statement, ast.Assign)
            and isinstance(statement.value, ast.Call)
            and isinstance(statement.value.func, ast.Attribute)
            and isinstance(statement.value.func.value, ast.Call)
            and isinstance(statement.value.func.value.func, ast.Name)
            and statement.value.func.value.func.id == "project_customer_network_map"
            for statement in node.body
        )
    ]
    assert len(projection_gates) == 1
    condition = projection_gates[0].test
    assert isinstance(condition, ast.BoolOp) and isinstance(condition.op, ast.And)
    assert [ast.unparse(predicate) for predicate in condition.values] == [
        "include_network",
        "map_data",
        "primary_address",
        "primary_address.latitude is not None",
        "primary_address.longitude is not None",
    ]
