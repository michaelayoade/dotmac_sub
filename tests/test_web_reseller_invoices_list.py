"""Reseller account-invoices list is routed through list_query."""

from __future__ import annotations

from app.services import reseller_portal, web_reseller_routes


def test_reseller_invoice_list_definition_declares_its_capabilities():
    definition = web_reseller_routes.RESELLER_INVOICE_LIST_DEFINITION
    assert set(definition.sortable_keys) == {"status", "total", "due_at", "created_at"}
    assert definition.default_sort == "created_at"
    assert definition.default_per_page == 25
    assert 25 in definition.per_page_options


def test_invoice_definition_sort_keys_are_backed_by_the_service_whitelist():
    # Every column the UI offers to sort by must be one the service can order on,
    # or the sort would silently fall back to the default.
    for key in web_reseller_routes.RESELLER_INVOICE_LIST_DEFINITION.sortable_keys:
        assert key in reseller_portal.ACCOUNT_INVOICE_SORT_COLUMNS


def test_invoice_definition_round_trips_state_in_the_url():
    query = web_reseller_routes.RESELLER_INVOICE_LIST_DEFINITION.build_query(
        search=None,
        filters={},
        sort_by="total",
        sort_dir="asc",
        page=1,
        per_page=25,
    )
    url = query.url("/reseller/accounts/abc/invoices", page=3)
    assert "sort=total" in url
    assert "dir=asc" in url
    assert "page=3" in url
