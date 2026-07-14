from __future__ import annotations

import ast
from pathlib import Path

from fastapi.templating import Jinja2Templates

from app.services.list_query import PageMeta
from app.services.status_presentation import account_status_presentation
from app.services.web_customer_lists import build_customer_list_query

PROJECT_ROOT = Path(__file__).resolve().parents[1]


def test_customer_route_delegates_query_normalization_to_list_owner():
    route_path = PROJECT_ROOT / "app/web/admin/customers.py"
    tree = ast.parse(route_path.read_text(encoding="utf-8"))
    route = next(
        node
        for node in tree.body
        if isinstance(node, ast.FunctionDef) and node.name == "customers_list"
    )

    calls = {
        ast.unparse(node.func) for node in ast.walk(route) if isinstance(node, ast.Call)
    }

    assert "web_customer_lists_service.build_customer_list_query" in calls
    assert "web_customer_lists_service.build_customers_index_context" in calls


def test_customer_table_consumes_contract_urls_and_accessibility_state():
    template = (PROJECT_ROOT / "templates/admin/customers/_table.html").read_text(
        encoding="utf-8"
    )

    assert "list_query.url('/admin/customers'" in template
    assert 'aria-sort="' in template
    assert 'role="status"' in template
    assert 'aria-live="polite"' in template
    assert 'aria-current="page"' in template
    assert 'aria-label="Select all customers on this page"' in template
    assert "/admin/customers?page=" not in template
    assert "range(1, total_pages + 1)" not in template
    assert "status_presentation_badge(customer.status_presentation" in template
    assert "customer.raw.status" not in template
    assert "cust_status" not in template
    assert (
        "{% if can_activate_subscriptions and "
        "customer.suspended_subscription_count %}" in template
    )
    assert (
        "{% if can_suspend_subscriptions and customer.active_subscription_count %}"
        in template
    )


def test_customer_status_surfaces_consume_semantic_presentation_owner():
    detail = (PROJECT_ROOT / "templates/admin/customers/detail.html").read_text(
        encoding="utf-8"
    )
    restricted = (
        PROJECT_ROOT / "templates/customer/dashboard/restricted.html"
    ).read_text(encoding="utf-8")
    mobile_chip = (PROJECT_ROOT / "mobile/lib/src/widgets/status_chip.dart").read_text(
        encoding="utf-8"
    )

    assert "customer_status_presentation" in detail
    assert "account_status_presentations" in detail
    assert "subscription_status_presentations" in detail
    assert "status_labels" not in detail
    assert "status_ring" not in detail
    assert "account_status_presentation" in restricted
    assert "account_status_display" not in restricted
    assert "StatusChip.fromPresentation" in mobile_chip
    assert "forSubscription" not in mobile_chip


def test_customer_filter_form_keeps_canonical_query_state_in_browser_history():
    template = (PROJECT_ROOT / "templates/admin/customers/index.html").read_text(
        encoding="utf-8"
    )

    assert 'hx-push-url="true"' in template
    assert 'name="sort" value="{{ list_query.sort_by }}"' in template
    assert 'name="dir" value="{{ list_query.sort_dir }}"' in template
    assert "e.detail.parameters.page = '1'" in template
    assert "stateSource.dataset.currentSort" in template
    assert "dynamic-table-config.js" not in template
    assert "data-dynamic-table" not in template
    assert "/api/v1/tables/customers" not in template


def test_legacy_customer_data_api_delegates_to_customer_list_owner():
    table_service = (PROJECT_ROOT / "app/services/table_config.py").read_text(
        encoding="utf-8"
    )
    table_api = (PROJECT_ROOT / "app/api/tables.py").read_text(encoding="utf-8")

    assert "_apply_customers_page_filters" not in table_service
    assert (
        "web_customer_lists.build_customer_list_query_from_legacy_params"
        in table_service
    )
    assert "web_customer_lists.build_customer_list_page" in table_service
    assert "TableConfigurationService.build_data_projection" in table_api


def test_subscriber_compatibility_api_delegates_without_a_live_parallel_screen():
    table_service = (PROJECT_ROOT / "app/services/table_config.py").read_text(
        encoding="utf-8"
    )
    subscriber_owner = (
        PROJECT_ROOT / "app/services/web_subscriber_lists.py"
    ).read_text(encoding="utf-8")
    templates = "\n".join(
        path.read_text(encoding="utf-8")
        for path in (PROJECT_ROOT / "templates").rglob("*.html")
    )

    assert "web_subscriber_lists.build_subscriber_list_page" in table_service
    assert "_ensure_subscriber_numbers" not in table_service
    assert "_apply_scalar_filters" not in table_service
    assert "subscriber_service.subscribers.query" in subscriber_owner
    assert 'data-table-key="subscribers"' not in templates
    assert 'data-dynamic-table="subscribers"' not in templates


def test_customer_table_contract_renders_with_empty_results():
    list_query = build_customer_list_query(
        search="missing",
        status=None,
        customer_type=None,
        nas_id=None,
        pop_site_id=None,
        sort_by="name",
        sort_dir="asc",
        page=1,
        per_page=25,
    )
    page_meta = PageMeta.from_query(list_query, total_items=0)
    templates = Jinja2Templates(directory=str(PROJECT_ROOT / "templates"))

    html = templates.env.get_template("admin/customers/_table.html").render(
        customers=[],
        list_query=list_query,
        page_meta=page_meta,
        search=list_query.search,
    )

    assert "No customers match the current search and filters." in html
    assert 'aria-sort="ascending"' in html
    assert "No customers found" in html


def test_semantic_status_macro_renders_owner_label_tone_and_icon():
    templates = Jinja2Templates(directory=str(PROJECT_ROOT / "templates"))
    template = templates.env.from_string(
        """
        {% from "components/ui/macros.html" import status_presentation_badge %}
        {{ status_presentation_badge(presentation, size="sm") }}
        """
    )

    html = template.render(
        presentation=account_status_presentation("suspended"),
    )

    assert "Suspended" in html
    assert "status-tone-warning" in html
    assert "M12 9v2" in html
    assert 'aria-label="Suspended status: warning"' in html
