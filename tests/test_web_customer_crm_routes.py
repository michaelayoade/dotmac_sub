from __future__ import annotations

import ast
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
ROUTES_PATH = REPO_ROOT / "app/web/customer/routes.py"


def _function_source(function_name: str) -> str:
    source = ROUTES_PATH.read_text()
    module = ast.parse(source)
    for node in module.body:
        if isinstance(node, ast.FunctionDef) and node.name == function_name:
            segment = ast.get_source_segment(source, node)
            if segment is not None:
                return segment
    raise AssertionError(f"Function {function_name} not found in routes.py")


def test_support_detail_redirect_preserves_ticket_next_url() -> None:
    source = _function_source("customer_support_detail")

    assert "quote_plus(ticket_id)" in source
    assert '"/portal/auth/login?next=/portal/support/' in source


def test_work_order_detail_redirect_preserves_work_order_next_url() -> None:
    source = _function_source("customer_work_order_detail")

    assert "quote_plus(work_order_id)" in source
    assert '"/portal/auth/login?next=/portal/work-orders/' in source


def test_support_create_failure_preserves_form_and_uses_error_template() -> None:
    source = _function_source("customer_support_create")

    assert '"customer/support/new.html"' in source
    assert "status_code=400" in source
    assert '"form_values"' in source
    assert '"/portal/support/{ticket_id}"' in source


def test_support_comment_failure_renders_detail_template_and_success_redirect() -> None:
    source = _function_source("customer_support_add_comment")

    assert '"customer/support/detail.html"' in source
    assert "status_code=400" in source
    assert '"/portal/support/{ticket_id}"' in source


def test_support_and_work_order_list_routes_delegate_to_crm_portal() -> None:
    support_source = _function_source("customer_support")
    work_orders_source = _function_source("customer_work_orders")

    assert "crm_portal.tickets_list_context" in support_source
    assert '"customer/support/index.html"' in support_source
    assert "crm_portal.work_orders_list_context" in work_orders_source
    assert '"customer/work-orders/index.html"' in work_orders_source
